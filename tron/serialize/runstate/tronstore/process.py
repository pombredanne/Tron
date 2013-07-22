# import time
import signal
import logging
import os
from multiprocessing import Process, Pipe

from tron.serialize.runstate.tronstore import tronstore
# from tron.serialize.runstate.tronstore.chunking import StoreChunkHandler

log = logging.getLogger(__name__)

class TronStoreError(Exception):
    """Raised whenever tronstore exits for an unknown reason."""
    def __init__(self, code):
        self.code = code
    def __str__(self):
        return repr(self.code)

class StoreProcessProtocol(object):
    """The class that actually communicates with tronstore. This is a subclass
    of the twisted ProcessProtocol class, which has a set of internals that can
    communicate with a child proccess via stdin/stdout via interrupts.

    Because of this I/O structure imposed by twisted, there are two types of
    messages: requests and responses. Responses are always of the same form,
    while requests have an enumerator (see msg_enums.py) to identify the
    type of request.
    """
    # This timeout MUST be longer than the one in tronstore!
    SHUTDOWN_TIMEOUT = 100.0
    POLL_TIMEOUT = 10.0

    def __init__(self, config, response_factory):
        self.config = config
        self.response_factory = response_factory
        self.orphaned_responses = {}
        self.is_shutdown = False
        self._start_process()

    def _start_process(self):
        """Spawn the tronstore process. The arguments given to tronstore must
        match the signature for tronstore.main.
        """
        self.pipe, child_pipe = Pipe()
        store_args = (self.config, child_pipe)

        # See the long comment in tronstore.main() as to why we have to do this
        # if not signal.getsignal(signal.SIGCHLD):
        #     print('registering child signal handler')
        #     signal.signal(signal.SIGCHLD, signal.getsignal(signal.SIGTERM))
        #     print('registered to %s' % signal.getsignal(signal.SIGCHLD))

        self.process = Process(target=tronstore.main, args=store_args)
        self.process.daemon = True
        self.process.start()

    def _verify_is_alive(self):
        """A check to verify that tronstore is alive. Attempts to restart
        tronstore if it finds that it exited for some reason."""
        if not self.process.is_alive():
            code = self.process.exitcode
            log.warn("tronstore exited prematurely with status code %d. Attempting to restart." % code)
            self._start_process()
            if not self.process.is_alive():
                raise TronStoreError("tronstore crashed with status code %d and failed to restart" % code)

    def send_request(self, request):
        """Send a StoreRequest to tronstore and immediately return without
        waiting for tronstore's response.
        """
        if self.is_shutdown:
            return
        self._verify_is_alive()

        self.pipe.send_bytes(request.serialized)
        # self.transport.write(self.chunker.sign(request.serialized))

    def _poll_for_response(self, id, timeout):
        """Polls for a response to the request with identifier id. Throws
        any responses that it isn't looking for into a dict, and tries to
        retrieve a matching response from this dict before pulling new
        responses.

        If Tron is extended into a synchronous program, simply just add a
        lock around this function ( with mutex.lock(): ) and everything'll
        be fine.
        """
        if id in self.orphaned_responses:
            response = self.orphaned_responses[id]
            del self.orphaned_responses[id]
            return response

        while self.pipe.poll(timeout):
            response = self.response_factory.rebuild(self.pipe.recv_bytes())
            if response.id == id:
                return response
            else:
                self.orphaned_responses[response.id] = response
        return None

    def send_request_get_response(self, request):
        """Send a StoreRequest to tronstore, and block until tronstore responds
        with the appropriate data. The StoreResponse is returned as is, with no
        modifications. Blocks for POLL_TIMEOUT seconds until returning None.
        """

        if self.is_shutdown:
            return self.response_factory.build(False, request.id, '')
        self._verify_is_alive()

        self.pipe.send_bytes(request.serialized)
        response = self._poll_for_response(request.id, self.POLL_TIMEOUT)
        if not response:
            log.warn("tronstore took longer than %d seconds to respond to a request, and it was dropped." % self.POLL_TIMEOUT)
            return self.response_factory.build(False, request.id, '')
        else:
            return response

    def send_request_shutdown(self, request):
        """Shut down the process protocol. Waits for SHUTDOWN_TIMEOUT seconds
        for tronstore to send a response, after which it kills both pipes
        and the process itself.

        Calling this prevents ANY further requests being made to tronstore, as
        the process will be terminated.
        """
        if self.is_shutdown or not self.process.is_alive():
            self.pipe.close()
            self.is_shutdown = True
            return
        self.is_shutdown = True

        self.pipe.send_bytes(request.serialized)
        response = self._poll_for_response(request.id, self.SHUTDOWN_TIMEOUT)

        if not response or not response.success:
            log.error("tronstore failed to shut down cleanly.")

        self.pipe.close()
        # We can't actually use process.terminate(), as that sends a SIGTERM
        # to the process, which unfortunately is registered to call the same
        # handler as trond due to how Python copies its environment over
        # to new processes. In addition, using process.terminate causes
        # SIGTERMs to get sent to everything that process spawns in its
        # route to shutting down- and when the trond event handler calls some
        # stuff that tronstore would never actually touch, and these calls
        # start some initialization related call stacks, this results in a
        # bunch of really strange call stacks all getting SIGTERMs that ALL end
        # up calling the trond signal handler, ending in this horrible
        # unclean shutdown.
        #
        # Using os.kill has the effect we actually want, which is to just
        # kill tronstore completely. We can do this safely at this point, as
        # tronstore ONLY sends the shutdown response when it's finished
        # all of its requests and shutting down the store object.
        os.kill(self.process.pid, signal.SIGKILL)

    def update_config(self, new_config, config_request):
        self.send_request(config_request)
        self.config = new_config
