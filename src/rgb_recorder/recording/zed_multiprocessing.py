"""This file is a slightly modified copy of airo-camera-toolkit's multiprocess_rgb_camera.
It explicitly checks whether a Zed camera was instantiated and supports reading either the left or the right view
of the camera."""

import multiprocessing
from multiprocessing import resource_tracker
import time
from multiprocessing import shared_memory
from typing import Optional, Tuple

import numpy as np
from airo_camera_toolkit.cameras.multiprocess.multiprocess_rgb_camera import shared_memory_block_like
from airo_camera_toolkit.cameras.zed.zed import Zed
from airo_camera_toolkit.interfaces import RGBCamera, StereoRGBDCamera
from airo_camera_toolkit.utils.image_converter import ImageConverter
from airo_typing import CameraResolutionType, NumpyFloatImageType, NumpyIntImageType, CameraIntrinsicsMatrixType
from loguru import logger

_RGB_LEFT_SHM_NAME = "rgb_left"
_RGB_RIGHT_SHM_NAME = "rgb_right"
_RGB_SHAPE_SHM_NAME = "rgb_shape"
_TIMESTAMP_SHM_NAME = "timestamp"
_INTRINSICS_SHM_NAME = "intrinsics"
_FPS_SHM_NAME = "fps"
# We use this flag as a lock (we can't use built-in events/locks because they need to be passed explicitly to the receivers.)
_WRITE_LOCK_SHM_NAME = "write_lock"  # Boolean: only one writer allowed at a time.
_READ_LOCK_SHM_NAME = "read_lock"  # int: number of readers currently reading.


class ZedPublisher(multiprocessing.context.SpawnProcess):
    """Publishes the data of a camera that implements the RGBCamera interface to shared memory blocks.
    Shared memory blocks can then be accessed in other processes using their names,
    cf. https://docs.python.org/3/library/multiprocessing.shared_memory.html#module-multiprocessing.shared_memory

    The Receiver class is a convenient way of doing so and is the intended way of using this class.
    """

    def __init__(
            self,
            camera_cls: type,
            camera_kwargs: dict = {},
            shared_memory_namespace: str = "camera",
            log_debug: bool = False,
    ):
        """Instantiates the publisher. Note that the publisher (and its process) will not start until start() is called.

        Args:
            camera_cls (type): The class e.g. Zed that this publisher will instantiate.
            camera_kwargs (dict, optional): The kwargs that will be passed to the camera_cls constructor.
            shared_memory_namespace (str, optional): The string that will be used to prefix the shared memory blocks that this class will create.
        """

        # context = multiprocessing.get_context("spawn")  # Default "fork" leads to CUDA issues.
        # Why it is a good idea in general to spawn: https://pythonspeed.com/articles/python-multiprocessing/

        super().__init__(daemon=True)
        self._shared_memory_namespace = shared_memory_namespace
        self._camera_cls = camera_cls
        self._camera_kwargs = camera_kwargs
        self._camera: Zed | None = None
        self.log_debug = log_debug
        self.running_event = multiprocessing.Event()
        self.shutdown_event = multiprocessing.Event()

        # Declare these here so mypy doesn't complain.
        self.rgb_left_shm: Optional[shared_memory.SharedMemory] = None
        self.rgb_right_shm: Optional[shared_memory.SharedMemory] = None
        self.rgb_shape_shm: Optional[shared_memory.SharedMemory] = None
        self.timestamp_shm: Optional[shared_memory.SharedMemory] = None
        self.intrinsics_shm: Optional[shared_memory.SharedMemory] = None
        self.fps_shm: Optional[shared_memory.SharedMemory] = None
        self.write_lock_shm: Optional[shared_memory.SharedMemory] = None
        self.read_lock_shm: Optional[shared_memory.SharedMemory] = None

        self.fps = None  # set in setup
        self.camera_period = None  # set in setup

    def start(self) -> None:
        """Starts the process. The process will not start until this method is called."""
        super().start()
        self.running_event.wait()  # Block until the publisher has started

    def _setup(self) -> None:
        """Note: to be able to retrieve camera image from the Publisher process, the camera must be instantiated in the
        Publisher process. For this reason, we do not instantiate the camera in __init__ but, here instead.

        We also create the shared memory blocks here, so that their lifetime is bound to the lifetime of the Publisher
        process. Usually shared memory may outlive its creator process, but as the Publisher is the only process that
        writes to the shared memory blocks, we want to make sure that they are deleted when the Publisher process is
        terminated. This also frees up the names of the shared memory blocks so that they can be reused.


        Five SharedMemory blocks are created, each block is prefixed with the namespace of the publisher. Three of these
        are only written once, the other two are written continuously.

        Constant blocks:
        * intrinsics: the intrinsics matrix of the camera
        * rgb_shape: the shape that rgb image array should be
        * fps: the fps of the camera

        Blocks that are written continuously:
        * rgb: the most recently retrieved image
        * timestamp: the timestamp of that image


        To simplify access, we create numpy arrays that are backed by the shared memory blocks for the rgb image and
        the intrinsics matrix.
        """

        # Instantiating a camera.
        logger.info(f"Instantiating a {self._camera_cls.__name__} camera.")
        self._camera = self._camera_cls(**self._camera_kwargs)
        assert isinstance(self._camera, Zed)  # Check whether user passed a valid camera class
        logger.info(f"Successfully instantiated a {self._camera_cls.__name__} camera.")

        rgb_left_name = f"{self._shared_memory_namespace}_{_RGB_LEFT_SHM_NAME}"
        rgb_right_name = f"{self._shared_memory_namespace}_{_RGB_RIGHT_SHM_NAME}"
        rgb_shape_name = f"{self._shared_memory_namespace}_{_RGB_SHAPE_SHM_NAME}"
        timestamp_name = f"{self._shared_memory_namespace}_{_TIMESTAMP_SHM_NAME}"
        intrinsics_name = f"{self._shared_memory_namespace}_{_INTRINSICS_SHM_NAME}"
        fps_name = f"{self._shared_memory_namespace}_{_FPS_SHM_NAME}"
        write_lock_name = f"{self._shared_memory_namespace}_{_WRITE_LOCK_SHM_NAME}"
        read_lock_name = f"{self._shared_memory_namespace}_{_READ_LOCK_SHM_NAME}"

        # Get the example arrays (this is the easiest way to initialize the shared memory blocks with the correct size).
        rgb = self._camera.get_rgb_image_as_int()  # We pass uint8 images as they consume 4x less memory
        rgb_shape = np.array(rgb.shape)
        logger.info(f"Successfully retrieved an image of shape {rgb.shape} from the camera.")

        timestamp = np.array([time.time()])
        intrinsics = self._camera.intrinsics_matrix()

        self.fps = self._camera.fps
        fps = np.array([self.fps], dtype=np.float64)
        self.camera_period = 1 / self.fps

        write_lock = np.array([False], dtype=np.bool_)
        read_lock = np.array([0], dtype=np.int_)

        # Create the shared memory blocks and numpy arrays that are backed by them.
        logger.info("Creating RGB shared memory blocks.")
        self.rgb_left_shm, self.rgb_left_shm_array = shared_memory_block_like(rgb, rgb_left_name)
        self.rgb_right_shm, self.rgb_right_shm_array = shared_memory_block_like(rgb, rgb_right_name)
        self.rgb_shape_shm, self.rgb_shape_shm_array = shared_memory_block_like(rgb_shape, rgb_shape_name)
        self.timestamp_shm, self.timestamp_shm_array = shared_memory_block_like(timestamp, timestamp_name)
        self.intrinsics_shm, self.intrinsics_shm_array = shared_memory_block_like(intrinsics, intrinsics_name)
        self.fps_shm, self.fps_shm_array = shared_memory_block_like(fps, fps_name)
        self.write_lock_shm, self.write_lock_shm_array = shared_memory_block_like(write_lock, write_lock_name)
        self.read_lock_shm, self.read_lock_shm_array = shared_memory_block_like(read_lock, read_lock_name)

        logger.info("Created RGB shared memory blocks.")

    def stop(self) -> None:
        self.shutdown_event.set()

    def run(self) -> None:
        """Main loop of the process, runs until the process is terminated.

        Each iteration a new image is retrieved from the camera and copied to the shared memory block.

        Note that we update timestamp after image data has been copied. This ensure that if the receiver sees a new
        timestamp, it will also see the new image data. Theoretically it is possble that the recevier reads new image
        data, but the timestamp is still old. I'm not sure whether this is a problem in practice.

        # TODO: invesitgate whether a Lock is required when copying the image data to the shared memory block. Also
        whether it is possible to do this without having to spawn all processes from a single Python script (e.g. to
        pass the Lock object).
        """

        logger.info(f"{self.__class__.__name__} process started.")
        self._setup()
        assert isinstance(self._camera, RGBCamera)  # Just to make mypy happy, already checked in _setup()
        logger.info(f'{self.__class__.__name__} starting to publish to "{self._shared_memory_namespace}".')

        try:
            while not self.shutdown_event.is_set():
                self._camera._grab_images()

                # Retrieve an image from the camera
                image_left = self._camera._retrieve_rgb_image_as_int(view=StereoRGBDCamera.LEFT_RGB)
                image_right = self._camera._retrieve_rgb_image_as_int(view=StereoRGBDCamera.RIGHT_RGB)

                # Wait to write to the shared memory block until there are no active readers (or writers).
                # (Normally we should be the only writer though.)
                while self.read_lock_shm_array[0] > 0 and self.write_lock_shm_array[0]:
                    time.sleep(0.00001)
                self.write_lock_shm_array[0] = True

                self.rgb_left_shm_array[:] = image_left[:]
                self.rgb_right_shm_array[:] = image_right[:]
                self.timestamp_shm_array[0] = time.time()
                self.write_lock_shm_array[0] = False
                self.running_event.set()
        except Exception as e:
            logger.error(f"Error in {self.__class__.__name__}: {e}")
        finally:
            self.unlink_shared_memory()
            logger.info(f"{self.__class__.__name__} process terminated.")
        self.unlink_shared_memory()

    def unlink_shared_memory(self) -> None:
        """Cleanup of the SharedMemory as recommended by the docs:
        https://docs.python.org/3/library/multiprocessing.shared_memory.html

        For debugging, use:
        watch -n 0.1 ls /dev/shm/

        However, I'm not sure how essential this actually is.
        """
        print(f"Unlinking RGB shared memory blocks of {self.__class__.__name__}.")

        if self.rgb_left_shm is not None:
            self.rgb_left_shm.close()
            self.rgb_left_shm.unlink()
            self.rgb_left_shm = None

        if self.rgb_right_shm is not None:
            self.rgb_right_shm.close()
            self.rgb_right_shm.unlink()
            self.rgb_right_shm = None

        if self.rgb_shape_shm is not None:
            self.rgb_shape_shm.close()
            self.rgb_shape_shm.unlink()
            self.rgb_shape_shm = None

        if self.timestamp_shm is not None:
            self.timestamp_shm.close()
            self.timestamp_shm.unlink()
            self.timestamp_shm = None

        if self.intrinsics_shm is not None:
            self.intrinsics_shm.close()
            self.intrinsics_shm.unlink()
            self.intrinsics_shm = None

        if self.fps_shm is not None:
            self.fps_shm.close()
            self.fps_shm.unlink()
            self.fps_shm = None

        if self.write_lock_shm is not None:
            self.write_lock_shm.close()
            self.write_lock_shm.unlink()
            self.write_lock_shm = None

        if self.read_lock_shm is not None:
            self.read_lock_shm.close()
            self.read_lock_shm.unlink()
            self.read_lock_shm = None

    def __del__(self) -> None:
        self.unlink_shared_memory()


class ZedReceiver(RGBCamera):
    """Implements the RGBD camera interface for a camera that is running in a different process and shares its data using shared memory blocks.
    To be used with the Publisher class.
    """

    def __init__(
        self,
        shared_memory_namespace: str,
    ) -> None:
        super().__init__()

        self._shared_memory_namespace = shared_memory_namespace
        rgb_left_name = f"{self._shared_memory_namespace}_{_RGB_LEFT_SHM_NAME}"
        rgb_right_name = f"{self._shared_memory_namespace}_{_RGB_RIGHT_SHM_NAME}"
        rgb_shape_name = f"{self._shared_memory_namespace}_{_RGB_SHAPE_SHM_NAME}"
        timestamp_name = f"{self._shared_memory_namespace}_{_TIMESTAMP_SHM_NAME}"
        intrinsics_name = f"{self._shared_memory_namespace}_{_INTRINSICS_SHM_NAME}"
        fps_name = f"{self._shared_memory_namespace}_{_FPS_SHM_NAME}"
        write_lock_name = f"{self._shared_memory_namespace}_{_WRITE_LOCK_SHM_NAME}"
        read_lock_name = f"{self._shared_memory_namespace}_{_READ_LOCK_SHM_NAME}"

        # Attach to existing shared memory blocks. Retry a few times to give the publisher time to start up (opening
        # connection to a camera can take a while).

        self.rgb_left_shm = shared_memory.SharedMemory(name=rgb_left_name)
        self.rgb_right_shm = shared_memory.SharedMemory(name=rgb_right_name)
        self.rgb_shape_shm = shared_memory.SharedMemory(name=rgb_shape_name)
        self.timestamp_shm = shared_memory.SharedMemory(name=timestamp_name)
        self.intrinsics_shm = shared_memory.SharedMemory(name=intrinsics_name)
        self.fps_shm = shared_memory.SharedMemory(name=fps_name)
        self.write_lock_shm = shared_memory.SharedMemory(name=write_lock_name)
        self.read_lock_shm = shared_memory.SharedMemory(name=read_lock_name)

        logger.info(f'SharedMemory namespace "{self._shared_memory_namespace}" found.')

        # Normally, we wouldn't have to do this unregistering. However, without it, the resource tracker incorrectly
        # destroys access to the shared memory blocks when the process is terminated. This is a known 3 year old bug
        # that hasn't been resolved yet: https://bugs.python.org/issue39959
        # Concretely, the problem was that once any MultiprocessRGBReceiver object was destroyed, all further access to
        # the shared memory blocks would fail with a FileNotFoundError.
        # We also ignore mypy telling us to use .name instead of ._name, because the latter is used in the registration.
        resource_tracker.unregister(self.rgb_left_shm._name, "shared_memory")  # type: ignore[attr-defined]
        resource_tracker.unregister(self.rgb_right_shm._name, "shared_memory")  # type: ignore[attr-defined]
        resource_tracker.unregister(self.rgb_shape_shm._name, "shared_memory")  # type: ignore[attr-defined]
        resource_tracker.unregister(self.intrinsics_shm._name, "shared_memory")  # type: ignore[attr-defined]
        resource_tracker.unregister(self.timestamp_shm._name, "shared_memory")  # type: ignore[attr-defined]
        resource_tracker.unregister(self.fps_shm._name, "shared_memory")  # type: ignore[attr-defined]
        resource_tracker.unregister(self.write_lock_shm._name, "shared_memory")  # type: ignore[attr-defined]
        resource_tracker.unregister(self.read_lock_shm._name, "shared_memory")  # type: ignore[attr-defined]

        # Timestamp and intrinsics are the same shape for all images, so I decided that we could hardcode their shape.
        # However, images come in many shapes, which I also decided to pass via shared memory. (Previously, I required
        # the image resolution to be passed to this class' constructor, but that was inconvenient to keep in sync
        # between publisher and receiver scripts.)
        # Create numpy arrays that are backed by the shared memory blocks
        self.rgb_shape_shm_array: np.ndarray = np.ndarray((3,), dtype=np.int64, buffer=self.rgb_shape_shm.buf)
        self.intrinsics_shm_array: np.ndarray = np.ndarray((3, 3), dtype=np.float64, buffer=self.intrinsics_shm.buf)
        self.timestamp_shm_array: np.ndarray = np.ndarray((1,), dtype=np.float64, buffer=self.timestamp_shm.buf)
        self.fps_shm_array: np.ndarray = np.ndarray((1,), dtype=np.float64, buffer=self.fps_shm.buf)
        self.write_lock_shm_array: np.ndarray = np.ndarray((1,), dtype=np.bool_, buffer=self.write_lock_shm.buf)
        self.read_lock_shm_array: np.ndarray = np.ndarray((1,), dtype=np.int_, buffer=self.read_lock_shm.buf)

        self.fps = self.fps_shm_array[0]

        # The shape of the image is not known in advance, so we need to retrieve it from the shared memory block.
        rgb_shape = tuple(self.rgb_shape_shm_array[:])
        self.rgb_left_shm_array: np.ndarray = np.ndarray(rgb_shape, dtype=np.uint8, buffer=self.rgb_left_shm.buf)
        self.rgb_right_shm_array: np.ndarray = np.ndarray(rgb_shape, dtype=np.uint8, buffer=self.rgb_right_shm.buf)

        # Preallocate the buffer array to avoid reallocation at each retrieve.
        self.rgb_left_buffer_array: np.ndarray = np.ndarray(rgb_shape, dtype=np.uint8)
        self.rgb_right_buffer_array: np.ndarray = np.ndarray(rgb_shape, dtype=np.uint8)

        self.previous_timestamp = time.time()

    def get_current_timestamp(self) -> float:
        """Timestamp of the image that is currently in the shared memory block.

        Warning: our current implementation, in theory the image and the timestamp could be out of sync when reading.
        Having atomic read/writes of both the image and timestap (a la ROS) would solve this.
        """
        return self.timestamp_shm_array[0]

    @property
    def resolution(self) -> CameraResolutionType:
        """The resolution of the camera, in pixels."""
        shape_array = [int(x) for x in self.rgb_shape_shm_array[:2]]
        return (shape_array[1], shape_array[0])

    def _grab_images(self) -> None:
        # logger.info(
        #     f"Current timestamp: {self.get_current_timestamp():.3f}, previous timestamp: {self.previous_timestamp:.3f}"
        # )
        while not self.get_current_timestamp() > self.previous_timestamp:
            time.sleep(0.0001)
        self.previous_timestamp = self.get_current_timestamp()
        # logger.debug(f"Updating timestamp: {self.previous_timestamp:.3f}")

    def _retrieve_rgb_image(self) -> Tuple[NumpyFloatImageType, NumpyFloatImageType]:
        # No need to check writing lock here because the _retrieve_rgb_image_as_int method does it.
        image_left, image_right = self._retrieve_rgb_image_as_int()
        image_left = ImageConverter.from_numpy_int_format(image_left).image_in_numpy_format
        image_right = ImageConverter.from_numpy_int_format(image_right).image_in_numpy_format
        return image_left, image_right

    def _retrieve_rgb_image_as_int(self) -> Tuple[NumpyIntImageType, NumpyIntImageType]:
        while self.write_lock_shm_array[0]:
            time.sleep(0.00001)
        self.read_lock_shm_array[0] += 1
#         logger.info(f"{self.rgb_left_shm_array.mean()}, {self.rgb_right_shm_array.mean()}")
        self.rgb_left_buffer_array[:] = self.rgb_left_shm_array[:]
        self.rgb_right_buffer_array[:] = self.rgb_right_shm_array[:]
        self.read_lock_shm_array[0] -= 1
        return self.rgb_left_buffer_array, self.rgb_right_buffer_array

    def intrinsics_matrix(self) -> CameraIntrinsicsMatrixType:
        return self.intrinsics_shm_array

    def _close_shared_memory(self) -> None:
        """Signal that the shared memory blocks are no longer needed from this process."""
        print(f"Closing RGB shared memory blocks of {self.__class__.__name__}.")

        if self.rgb_left_shm is not None:
            self.rgb_left_shm.close()
            self.rgb_left_shm = None  # type: ignore

        if self.rgb_right_shm is not None:
            self.rgb_right_shm.close()
            self.rgb_right_shm = None  # type: ignore

        if self.rgb_shape_shm is not None:
            self.rgb_shape_shm.close()
            self.rgb_shape_shm = None  # type: ignore

        if self.timestamp_shm is not None:
            self.timestamp_shm.close()
            self.timestamp_shm = None  # type: ignore

        if self.intrinsics_shm is not None:
            self.intrinsics_shm.close()
            self.intrinsics_shm = None  # type: ignore

        if self.fps_shm is not None:
            self.fps_shm.close()
            self.fps_shm = None  # type: ignore

        if self.write_lock_shm is not None:
            self.write_lock_shm.close()
            self.write_lock_shm = None  # type: ignore

        if self.read_lock_shm is not None:
            self.read_lock_shm.close()
            self.read_lock_shm = None  # type: ignore

    def __del__(self) -> None:
        self._close_shared_memory()