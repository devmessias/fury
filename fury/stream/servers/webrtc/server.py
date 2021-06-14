# import os
# os.environ['PYTHONASYNCIODEBUG'] = '1'
import logging
import sys
if sys.version_info.minor >= 8:
    from multiprocessing import shared_memory
    PY_VERSION_8 = True
else:
    shared_memory = None
    PY_VERSION_8 = False

from aiohttp import web
from av import VideoFrame
from aiortc import VideoStreamTrack
import numpy as np

from fury.stream.servers.webrtc.async_app import get_app
from fury.stream.tools import CircularQueue

try:
    import pyximport
    pyximport.install()
    from fury.stream.servers.webrtc.FuryVideoFrame import FuryVideoFrame
    CYTHON_AVAILABLE = True
except ImportError:
    CYTHON_AVAILABLE = False


def webrtc_server(
        stream_client=None,
        image_buffers=None,
        image_buffer_names=None,
        info_buffer=None,
        info_buffer_name=None,
        circular_queue=None,
        queue_head_tail_buffer=None,
        queue_buffer=None,
        queue_head_tail_buffer_name=None,
        queue_buffer_name=None,
        port=8000, host='localhost',
        ):
   

    if stream_client is not None:
        image_buffers = stream_client.image_buffers
        info_buffer = stream_client.info_buffer

    use_raw_array = image_buffer_names is None and info_buffer_name is None

    class RTCServer(VideoStreamTrack):
        def __init__(
                self, use_raw_array=True,
                info_buffer=None, image_buffers=None,
                info_buffer_name=None, image_buffer_names=None):
            super().__init__()

            self.frame = None
            self.use_raw_array = use_raw_array
            if not use_raw_array:
                self.info_buffer = shared_memory.SharedMemory(info_buffer_name)
                self.info_buffer_repr = np.ndarray(
                        6,
                        dtype='uint64',
                        buffer=self.info_buffer.buf)
                self.image_buffers = []
                self.image_reprs = []
                self.image_buffer_names = image_buffer_names
                for buffer_name in self.image_buffer_names:
                    buffer = shared_memory.SharedMemory(buffer_name)
                    self.image_buffers.append(buffer)
                    self.image_reprs.append(np.ndarray(
                        len(buffer.buf),
                        dtype=np.uint8,
                        buffer=buffer.buf))
            else:
                self.info_buffer = np.frombuffer(
                    info_buffer, 'uint64')
                self.info_buffer_repr = np.ctypeslib.as_array(
                    self.info_buffer)
                self.image_buffers = image_buffers

        async def recv(self):
            pts, time_base = await self.next_timestamp()
            if self.use_raw_array:
                image_info = np.frombuffer(
                    self.info_buffer, 'uint32')
            else:
                image_info = self.info_buffer_repr

            buffer_index = int(image_info[1])
            width = int(image_info[2+buffer_index*2])
            height = int(image_info[2+buffer_index*2+1])
            if self.frame is None \
                or self.frame.planes[0].width != width \
                    or self.frame.planes[0].height != height:
                if CYTHON_AVAILABLE:
                    self.frame = FuryVideoFrame(width, height, "rgb24")
                # else:
                #    self.frame = VideoFrame(width, height, "rgb24")
            if use_raw_array:
                self.image = self.image_buffers[buffer_index]
            else:
                self.image = self.image_reprs[buffer_index]

            if not CYTHON_AVAILABLE:
                # if the buffer it's already flipped
                # self.frame.planes[0].update(self.image)
                self.image = np.frombuffer(
                         self.image,
                         'uint8'
                     )[0:width*height*3].reshape((height, width, 3))
                self.image = np.flipud(self.image)
     
                self.frame = VideoFrame.from_ndarray(self.image)
            else:
                self.frame.update_from_buffer(self.image)

            self.frame.pts = pts
            self.frame.time_base = time_base

            return self.frame

        def release(self):

            logging.info("Release Server")
            try:
                if not (self.stream is None):
                    self.stream.release()
                    self.stream = None
            except AttributeError:
                pass

        def cleanup(self):
            logging.info("Freeing buffer from RTC Server")
            self.release()
            if not self.use_raw_array:
                for buffer in self.image_buffers:
                    #buffer.close()
                    ...
                #self.info_buffer.close()

    if queue_buffer is not  None or queue_buffer_name is not None:
        circular_queue = CircularQueue(
            head_tail_buffer=queue_head_tail_buffer,
            buffer=queue_buffer,
            buffer_name=queue_buffer_name,
            head_tail_buffer_name=queue_head_tail_buffer_name)

    rtc_server = RTCServer(
        use_raw_array,
        info_buffer, image_buffers,
        info_buffer_name, image_buffer_names)

    app_fury = get_app(
       rtc_server, circular_queue=circular_queue,
    )

    web.run_app(
        app_fury, host=host, port=port, ssl_context=None)
    rtc_server.cleanup()
    if circular_queue is not None:
        circular_queue.cleanup()

def interaction_server(
        circular_queue=None,
        queue_head_tail_buffer=None,
        queue_buffer=None,
        port=8080, host='localhost',
        ):

    if circular_queue is None and queue_buffer is not None:
        circular_queue = CircularQueue(
            head_tail_buffer=queue_head_tail_buffer,
            buffers_list=queue_buffer)

    app_fury = get_app(
        None, circular_queue=circular_queue)

    web.run_app(
        app_fury, host=host, port=port, ssl_context=None)
