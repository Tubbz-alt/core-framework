#
# This file is protected by Copyright. Please refer to the COPYRIGHT file
# distributed with this source distribution.
#
# This file is part of REDHAWK bulkioInterfaces.
#
# REDHAWK bulkioInterfaces is free software: you can redistribute it and/or modify it under
# the terms of the GNU Lesser General Public License as published by the Free
# Software Foundation, either version 3 of the License, or (at your option) any
# later version.
#
# REDHAWK bulkioInterfaces is distributed in the hope that it will be useful, but WITHOUT
# ANY WARRANTY; without even the implied warranty of MERCHANTABILITY or FITNESS
# FOR A PARTICULAR PURPOSE.  See the GNU Lesser General Public License for more
# details.
#
# You should have received a copy of the GNU Lesser General Public License
# along with this program.  If not, see http://www.gnu.org/licenses/.
#


import threading
import collections
import copy
import time

from ossie.utils import uuid
from ossie.cf.CF import Port
from ossie.utils.notify import notification
from ossie.utils.log4py import logging

from bulkio.statistics import InStats
import bulkio.sri
from bulkio import timestamp
from bulkio import const
from bulkio.in_stream import *
from bulkio.bulkioInterfaces import BULKIO, BULKIO__POA

class Packet(object):
    def __init__(self, data, T, EOS, SRI, sriChanged, inputQueueFlushed):
        self.buffer = data
        self.T = T
        self.EOS = EOS
        self.SRI = SRI
        self.sriChanged = sriChanged
        self.inputQueueFlushed = inputQueueFlushed
        self.streamID = SRI.streamID

class InPort:
    DATA_BUFFER=0
    TIME_STAMP=1
    END_OF_STREAM=2
    STREAM_ID=3
    SRI=4
    SRI_CHG=5
    QUEUE_FLUSH=6
    _TYPE_ = 'c'

    # Backwards-compatible DataTransfer type can still be unpacked like a tuple
    # but also supports named fields
    DataTransfer = collections.namedtuple('DataTransfer', 'dataBuffer T EOS streamID SRI sriChanged inputQueueFlushed')

    def __init__(self, name, logger=None, sriCompare=bulkio.sri.compare, newSriCallback=None, sriChangeCallback=None,  maxsize=100, PortTransferType=_TYPE_, bits=0):
        self.name = name
        self.logger = logger
        self.queue = collections.deque()
        self._maxSize = maxsize
        self._breakBlock = False
        # Backwards-compatibility: use the transfer type and let InStats figure
        # out the number of bits
        if bits == 0:
            self.stats = InStats(name, PortTransferType)
        else:
            self.stats = InStats(name, bits=bits)
        self.blocking = False
        self.sri_cmp = sriCompare
        self.newSriCallback = newSriCallback
        self.sriChangeCallback = sriChangeCallback
        self.sriDict = {} # key=streamID, value=StreamSRI

        self._dataBufferLock = threading.Lock()
        self._dataAvailable = threading.Condition(self._dataBufferLock)
        self._queueAvailable = threading.Condition(self._dataBufferLock)

        # Backwards-compatibility
        self.port_lock = self._dataBufferLock

        # Synchronizes access to the SRIs
        self._sriUpdateLock = threading.Lock()

        # Streams that are currently active (map of streamID to stream objects)
        self._streams = {}
        self._streamsMutex = threading.Lock()

        # Streams that have the same stream ID as an active stream, when an
        # end-of-stream has been queued but not yet read (each entry in the map
        # is a list of stream objects)
        self._pendingStreams = {}

        if logger==None:
            self.logger = logging.getLogger("redhawk.bulkio.input."+name)

        _cmpMsg  = "DEFAULT"
        _newSriMsg  = "EMPTY"
        _sriChangeMsg  = "EMPTY"
        if sriCompare != bulkio.sri.compare:
            _cmpMsg  = "USER_DEFINED"
        if newSriCallback:
            _newSriMsg  = "USER_DEFINED"
        if sriChangeCallback:
            _sriChangeMsg  = "USER_DEFINED"

        if self.logger:
            self.logger.debug( "bulkio::InPort CTOR port:" + str(name) +
                          " Blocking/MaxInputQueueSize " + str(self.blocking) + "/"  + str(maxsize) +
                          " SriCompare/NewSriCallback/SriChangeCallback " +  _cmpMsg + "/" + _newSriMsg + "/" + _sriChangeMsg );

    @notification
    def streamAdded(self, stream):
        """
        A new input stream was received.
        """
        pass

    def addStreamListener(self, callback):
        self.streamAdded.addListener(callback)

    def removeStreamListener(self, callbac):
        self.streamAdded.removeListener(callback)

    def setNewSriListener(self, newSriCallback):
        with self._sriUpdateLock:
            self.newSriCallback = newSriCallback

    def setSriChangeListener(self, sriChangeCallback):
        with self._sriUpdateLock:
            self.sriChangeCallback = sriChangeCallback

    def enableStats(self, enabled):
        self.stats.setEnabled(enabled)

    def _get_statistics(self):
        with self._dataBufferLock:
            return self.stats.retrieve()

    def _get_state(self):
        with self._dataBufferLock:
            if len(self.queue) == 0:
                return BULKIO.IDLE
            elif len(self.queue) == self._maxSize:
                return BULKIO.BUSY
            else:
                return BULKIO.ACTIVE

    def _get_activeSRIs(self):
        with self._sriUpdateLock:
            return [self.sriDict[entry][0] for entry in self.sriDict]

    def getCurrentQueueDepth(self):
        with self._dataBufferLock:
            return len(self.queue)

    def getMaxQueueDepth(self):
        with self._dataBufferLock:
            return self._maxSize

    #set to -1 for infinite queue
    def setMaxQueueDepth(self, newDepth):
        with self._dataBufferLock:
            self._maxSize = int(newDepth)

    def unblock(self):
        with self._dataBufferLock:
            self._breakBlock = False

    def block(self):
        # Interrupt packet queue operations
        with self._dataBufferLock:
            self._breakBlock = True
            self._dataAvailable.notifyAll()
            self._queueAvailable.notifyAll()

    # Provide standard interface for start/stop
    startPort = unblock
    stopPort = block

    def pushSRI(self, H):

        if self.logger:
            self.logger.trace( "bulkio::InPort pushSRI ENTER (port=" + str(self.name) +")" )

        # If the updated SRI is blocking, ensure port blocking mode is set
        if H.blocking:
            with self._dataBufferLock:
                self.blocking = True

        with self._sriUpdateLock:
            if H.streamID not in self.sriDict:
                new_stream = True
                sri_changed = True
                if self.logger:
                    self.logger.debug( "pushSRI PORT:" + str(self.name) + " NEW SRI:" + str(H.streamID) )
                if self.newSriCallback:
                    self.newSriCallback(H)
            else:
                new_stream = False
                sri, sri_changed = self.sriDict[H.streamID]
                if self.sri_cmp and not self.sri_cmp(sri, H):
                    sri_changed = True
                    if self.sriChangeCallback:
                        self.sriChangeCallback(H)

            if sri_changed:
                self.sriDict[H.streamID] = (copy.deepcopy(H), True)

        if new_stream:
            self._createStream(H)

        if self.logger:
            self.logger.trace( "bulkio::InPort pushSRI EXIT (port=" + str(self.name) +")" )

    def getPacket(self, timeout=const.NON_BLOCKING):
        if self.logger:
            self.logger.trace( "bulkio::InPort getPacket ENTER (port=" + str(self.name) +")" )

        packet = self._nextPacket(timeout)
        if not packet:
            # Return an empty packet instead of None for backwards
            # compatibility
            packet = InPort.DataTransfer(None, None, None, None, None, None, None)
        else:
            packet = InPort.DataTransfer(packet.buffer, packet.T, packet.EOS, packet.streamID, packet.SRI, packet.sriChanged, packet.inputQueueFlushed)

        if self.logger:
            self.logger.trace( "bulkio::InPort getPacket EXIT (port=" + str(self.name) +")" )

        return packet

    def getCurrentStream(self, timeout=const.BLOCKING):
        """
        Gets the stream that should be used for the next basic read.
        """
        # Prefer a stream that already has buffered data
        with self._streamsMutex:
            for stream in self._streams.itervalues():
                if stream._hasBufferedData():
                    return stream

        # Otherwise, return the stream that owns the next packet on the queue,
        # potentially waiting for one to be received
        with self._dataBufferLock:
            if self.queue:
                packet = self._peekPacket(timeout)
                return self.getStream(packet.streamID)

        return None

    def getStream(self, streamID):
        """
        Gets the active stream with the given stream ID.
        """
        with self._streamsMutex:
            return self._streams.get(streamID, None)

    def getStreams(self):
        """
        Returns the current set of active streams.
        """
        with self._streamsMutex:
            return self._streams.values()

    def pushPacket(self, data, T, EOS, streamID):
        self.logger.trace("pushPacket ENTER")
        self._queuePacket(data, T, EOS, streamID)
        self.logger.trace("pushPacket EXIT")

    def _queuePacket(self, data, T, EOS, streamID):
        # Discard packets for disabled streams
        if not self._acceptPacket(streamID, EOS):
            if EOS and self._disableBlockingOnEOS(streamID):
                # If this was the only blocking stream, turn off blocking
                with self._dataBufferLock:
                    self.blocking = False;
            return

        if self._maxSize == 0:
            return

        new_stream = False
        with self._sriUpdateLock:
            sri, sri_changed = self.sriDict.get(streamID, (None, True))
            if not sri:
                # Unknown stream ID, register a new default SRI following the
                # logic in pushSRI
                self.logger.warn("received data for stream '%s' with no SRI", streamID)
                new_stream = True
                sri = bulkio.sri.create(streamID)
                if self.newSriCallback:
                    self.newSriCallback(sri)

            # Acknowledge SRI change was received; in the unknown stream case,
            # this also records it in the map
            self.sriDict[streamID] = (sri, False)

        # If a new stream needs to be created for an unrecognized stream ID, do
        # it here after the lock is released
        if new_stream:
            self._createStream(sri)

        with self._dataBufferLock:
            queue_flushed = False

            if self.blocking:
                while len(self.queue) >= self._maxSize:
                    self._queueAvailable.wait()
            else:
                # Flush the queue if not using infinite queue (maxSize < 0),
                # blocking is not on, and queue is currently full
                if len(self.queue) >= self._maxSize and self._maxSize > -1:
                    # Handle lost EOS and SRI changes. The original code simply
                    # searched through the queue for any such flags without
                    # taking streamID into account.
                    for packet in self.queue:
                        if EOS and sri_changed:
                            break
                        if packet.sriChanged:
                            sri_changed = True
                        if packet.EOS:
                            EOS = True

                    queue_flushed = True
                    self.queue.clear()

            self.logger.trace("bulkio::InPort pushPacket NEW Packet (QUEUE=%d)", len(self.queue))
            self.stats.update(self._packetSize(data), float(len(self.queue))/float(self._maxSize), EOS, streamID, queue_flushed)
            packet = Packet(data, T, EOS, sri, sri_changed, queue_flushed)
            self.queue.append(packet)

            # Let one waiting getPacket call know there is a packet available
            self._dataAvailable.notify()

    def _acceptPacket(self, streamID, EOS):
        # Acquire streamsMutex for the duration of this call to ensure that
        # end-of-stream is handled atomically for disabled streams
        with self._streamsMutex:
            # Find the current stream for the stream ID and check whether it's
            # enabled
            stream = self._streams.get(streamID, None);
            if not stream or stream.enabled():
                return True
      
            # If there's a pending stream, the packet is designated for that
            pending_streams = self._pendingStreams.get(streamID, [])
            if pending_streams:
                return True

            new_stream = None
            if EOS:
                # Acknowledge the end-of-stream by removing the disabled stream
                # before discarding the packet
                self.logger.debug("Removing stream '%s'", streamID);
                stream._close();
                del self._streams[streamID]

                if pending_streams:
                    self.logger.debug("Moving pending stream '%s' to active", streamID);
                    new_stream = pending_streams.pop(0)
                    self._streams[streamID] = new_stream

        # If a pending stream became active, notify listeners
        if new_stream:
            self.streamAdded(new_stream)

        return False;
            

    def _peekPacket(self, timeout):
        # Requires self._dataBufferLock
        to_time = time.time() + timeout
        while not self._breakBlock and not self.queue:
            if timeout == 0.0:
                break
            elif timeout > 0:
                wait_time = to_time - time.time()
                if wait_time <= 0.0:
                    break
                self._dataAvailable.wait(wait_time)
            else:
                print self._dataAvailable.wait()

        if self._breakBlock or not self.queue:
            return None
        else:
            return self.queue[0]

    def _nextPacket(self, timeout, streamID=None):
        if self._breakBlock:
            return None

        to_time = time.time() + timeout

        with self._dataBufferLock:
            packet = self._fetchPacket(streamID)
            while not packet:
                if timeout == 0.0:
                    return None
                elif timeout > 0.0:
                    wait_time = to_time - time.time()
                    if wait_time <= 0.0:
                        return None
                    self._dataAvailable.wait(wait_time)
                else:
                    self._dataAvailable.wait()
                if self._breakBlock:
                    return None
                packet = self._fetchPacket(streamID)

            #LOG_TRACE(logger, "InPort::nextPacket PORT:" << name << " (QUEUE="<< packetQueue.size() << ")");
            self._queueAvailable.notify()

        if packet and packet.EOS and self._disableBlockingOnEOS(packet.streamID):
            with self._dataBufferLock:
                self.blocking = False

        return packet

    def _fetchPacket(self, streamID):
        # Prerequisite: caller holds self._dataBufferLock
        if not streamID:
            if not self.queue:
                return None
            return self.queue.popleft()

        for index in xrange(len(self.queue)):
            if self.queue[index].streamID == streamID:
                packet = self.queue[index]
                del self.queue[index]
                return packet
        return None

    def _disableBlockingOnEOS(self, streamID):
        with self._sriUpdateLock:
            sri, _ = self.sriDict.pop(streamID, (None, None))
            if sri and sri.blocking:
                for hdr, _ in self.sriDict.itervalues():
                    if hdr.blocking:
                        return False
                return True

        return False

    def _createStream(self, sri):
        with self._streamsMutex:
            if sri.streamID in self._streams:
                # An active stream has the same stream ID; add this new stream
                # to the pending list
                self.logger.debug("Creating pending stream '%s'", sri.streamID)
                if not sri.streamID in self._pendingStreams:
                    self._pendingStreams[sri.streamID] = []
                self._pendingStreams[sri.streamID].append(sri)
                stream = None
            else:
                # New stream
                self.logger.debug("Creating new stream '%s'", sri.streamID)
                stream = self.StreamType(sri, self)
                self._streams[sri.streamID] = stream

        # Notify stream listeners (without the mutex held)
        if stream:
            self.streamAdded(stream)

    def _removeStream(self, streamID):
        self.logger.debug("Removing stream '%s'", streamID)

        new_stream = None
        with self._streamsMutex:
            # Remove the current stream, and if there's a pending stream with
            # the same stream ID, move it to the active list
            self._streams.pop(streamID, None);
            pending_streams = self._pendingStreams.get(streamID, [])
            if pending_streams:
                self.logger.debug("Moving pending stream '%s' active", streamID)
                new_stream = pending_streams.pop(0)
                self._streams[streamID] = new_stream

        if new_stream:
            self.streamAdded(stream);

    def _discardPacketsForStream(self, streamID):
        with self._dataBufferLock:
            self.queue = [pkt for pkt in self.queue if pkt.streamID != streamID]

    def _packetSize(self, data):
        return len(data)


class InCharPort(InPort, BULKIO__POA.dataChar):
    _TYPE_ = 'c'
    StreamType = BufferedInputStream

    def __init__(self, name, logger=None, sriCompare=bulkio.sri.compare, newSriCallback=None, sriChangeCallback=None, maxsize=100 ):
        InPort.__init__(self, name, logger, sriCompare, newSriCallback, sriChangeCallback, maxsize, InCharPort._TYPE_, bits=8)

class InOctetPort(InPort, BULKIO__POA.dataOctet):
    _TYPE_ = 'B'
    StreamType = BufferedInputStream

    def __init__(self, name, logger=None, sriCompare=bulkio.sri.compare, newSriCallback=None, sriChangeCallback=None, maxsize=100 ):
        InPort.__init__(self, name, logger, sriCompare, newSriCallback, sriChangeCallback, maxsize, InOctetPort._TYPE_, bits=8)

class InShortPort(InPort, BULKIO__POA.dataShort):
    _TYPE_ = 'h'
    StreamType = BufferedInputStream

    def __init__(self, name, logger=None, sriCompare=bulkio.sri.compare, newSriCallback=None, sriChangeCallback=None, maxsize=100 ):
        InPort.__init__(self, name, logger, sriCompare, newSriCallback, sriChangeCallback, maxsize, InShortPort._TYPE_, bits=16)

class InUShortPort(InPort, BULKIO__POA.dataUshort):
    _TYPE_ = 'H'
    StreamType = BufferedInputStream

    def __init__(self, name, logger=None, sriCompare=bulkio.sri.compare, newSriCallback=None, sriChangeCallback=None, maxsize=100 ):
        InPort.__init__(self, name, logger, sriCompare, newSriCallback, sriChangeCallback, maxsize, InUShortPort._TYPE_, bits=16)

class InLongPort(InPort, BULKIO__POA.dataLong):
    _TYPE_ = 'i'
    StreamType = BufferedInputStream

    def __init__(self, name, logger=None, sriCompare=bulkio.sri.compare, newSriCallback=None, sriChangeCallback=None, maxsize=100 ):
        InPort.__init__(self, name, logger, sriCompare, newSriCallback, sriChangeCallback, maxsize, InLongPort._TYPE_, bits=32)

class InULongPort(InPort, BULKIO__POA.dataUlong):
    _TYPE_ = 'I'
    StreamType = BufferedInputStream

    def __init__(self, name, logger=None, sriCompare=bulkio.sri.compare, newSriCallback=None, sriChangeCallback=None, maxsize=100 ):
        InPort.__init__(self, name, logger, sriCompare, newSriCallback, sriChangeCallback, maxsize, InULongPort._TYPE_, bits=32)

class InLongLongPort(InPort, BULKIO__POA.dataLongLong):
    _TYPE_ = 'q'
    StreamType = BufferedInputStream

    def __init__(self, name, logger=None, sriCompare=bulkio.sri.compare, newSriCallback=None, sriChangeCallback=None, maxsize=100 ):
        InPort.__init__(self, name, logger, sriCompare, newSriCallback, sriChangeCallback, maxsize, InLongLongPort._TYPE_, bits=64)


class InULongLongPort(InPort, BULKIO__POA.dataUlongLong):
    _TYPE_ = 'Q'
    StreamType = BufferedInputStream

    def __init__(self, name, logger=None, sriCompare=bulkio.sri.compare, newSriCallback=None, sriChangeCallback=None, maxsize=100 ):
        InPort.__init__(self, name, logger, sriCompare, newSriCallback, sriChangeCallback, maxsize, InULongLongPort._TYPE_, bits=64)


class InFloatPort(InPort, BULKIO__POA.dataFloat):
    _TYPE_ = 'f'
    StreamType = BufferedInputStream

    def __init__(self, name, logger=None, sriCompare=bulkio.sri.compare, newSriCallback=None, sriChangeCallback=None, maxsize=100 ):
        InPort.__init__(self, name, logger, sriCompare, newSriCallback, sriChangeCallback, maxsize, InFloatPort._TYPE_, bits=32)


class InDoublePort(InPort, BULKIO__POA.dataDouble):
    _TYPE_ = 'd'
    StreamType = BufferedInputStream

    def __init__(self, name, logger=None, sriCompare=bulkio.sri.compare, newSriCallback=None, sriChangeCallback=None, maxsize=100 ):
        InPort.__init__(self, name, logger, sriCompare, newSriCallback, sriChangeCallback, maxsize, InDoublePort._TYPE_, bits=64)


class InBitPort(InPort, BULKIO__POA.dataBit):
    _TYPE_ = 'B'
    StreamType = InBitStream

    def __init__(self, name, logger=None, sriCompare=bulkio.sri.compare, newSriCallback=None, sriChangeCallback=None, maxsize=100 ):
        InPort.__init__(self, name, logger, sriCompare, newSriCallback, sriChangeCallback, maxsize, InBitPort._TYPE_, bits=1)

    def _packetSize(self, data):
        return data.bits


class InFilePort(InPort, BULKIO__POA.dataFile):
    _TYPE_ = 'c'
    StreamType = InFileStream

    def __init__(self, name, logger=None, sriCompare=bulkio.sri.compare, newSriCallback=None, sriChangeCallback=None, maxsize=100 ):
        InPort.__init__(self, name, logger, sriCompare, newSriCallback, sriChangeCallback, maxsize, InFilePort._TYPE_, bits=8)

    def _packetSize(self, data):
        # For statistics, consider the entire URL a single element
        return 1


class InXMLPort(InPort, BULKIO__POA.dataXML):
    _TYPE_ = 'c'
    StreamType = InXMLStream

    def __init__(self, name, logger=None, sriCompare=bulkio.sri.compare, newSriCallback=None, sriChangeCallback=None, maxsize=100 ):
        InPort.__init__(self, name, logger, sriCompare, newSriCallback, sriChangeCallback, maxsize, InXMLPort._TYPE_, bits=8)

    def pushPacket(self, xml_string, EOS, streamID):
        # Insert a None for the timestamp and use parent implementation
        InPort.pushPacket(self, xml_string, None, EOS, streamID)

class InAttachablePort:
    _TYPE_='b'
    def __init__(self, name, logger=None, attachDetachCallback=None, sriCmp=bulkio.sri.compare, timeCmp=timestamp.compare, PortType = _TYPE_, newSriCallback=None, sriChangeCallback=None,interface=None):
        self.name = name
        self.logger = logger
        self.port_lock = threading.Lock()
        self.sri_query_lock = threading.Lock()
        self._attachedStreams = {} # key=attach_id, value = (streamDef, userid)
        self.stats = InStats(name, PortType )
        self.sriDict = {} # key=streamID, value=(StreamSRI, PrecisionUTCTime)
        self.attachDetachCallback = attachDetachCallback
        self.newSriCallback = newSriCallback
        self.sriChangeCallback = sriChangeCallback
        self.sri_cmp = sriCmp
        self.time_cmp = timeCmp
        self.sriChanged = False
        if not interface:
            if self.logger:
                self.logger.error("InAttachablePort __init__ - an interface must be specified, set to BULKIO.dataSDDS or BULKIO.dataVITA49")
            raise Port.InvalidPort(1, "InAttachablePort __init__ - an interface must be specified, set to BULKIO.dataSDDS or BULKIO.dataVITA49")
        self.interface=interface # BULKIO port interface (valid options are BULKIO.dataSDDS or BULKIO.dataVITA49)
        self.setNewAttachDetachListener(attachDetachCallback)
        if self.logger:
            self.logger.debug("bulkio::InAttachablePort CTOR port:" + str(self.name) + " using interface " + str(self.interface))

    def setNewAttachDetachListener(self, attachDetachCallback ):
        self.port_lock.acquire()
        try:
            self.attachDetachCallback = attachDetachCallback

            # Set _attach_cb
            try:
                self._attach_cb = getattr(attachDetachCallback, "attach")
                if not callable(self._attach_cb):
                    self._attach_cb = None
            except AttributeError:
                self._attach_cb = None

            # Set _detach_cb
            try:
                self._detach_cb = getattr(attachDetachCallback, "detach")
                if not callable(self._detach_cb):
                    self._detach_cb = None
            except AttributeError:
                self._detach_cb = None

        finally:
            self.port_lock.release()

    def setNewSriListener(self, newSriCallback):
        self.port_lock.acquire()
        try:
            self.newSriCallback = newSriCallback
        finally:
            self.port_lock.release()

    def setSriChangeListener(self, sriChangeCallback):
        self.port_lock.acquire()
        try:
            self.sriChangeCallback = sriChangeCallback
        finally:
            self.port_lock.release()

    def setBitSize(self, bitSize):
        self.stats.setBitSize(bitSize)

    def enableStats(self, enabled):
        self.stats.setEnabled(enabled)

    def updateStats(self, elementsReceived, queueSize, streamID):
        self.port_lock.acquire()
        try:
            self.stats.update(elementsReceived, queueSize, streamID)
        finally:
            self.port_lock.release()

    def _get_statistics(self):
        self.port_lock.acquire()
        try:
            recStat = self.stats.retrieve()
        finally:
            self.port_lock.release()
        return recStat

    def _get_state(self):
        self.port_lock.acquire()
        try:
            numAttachedStreams = len(self._attachedStreams.values())
        finally:
            self.port_lock.release()
        if numAttachedStreams == 0:
            return BULKIO.IDLE
        # default behavior is to limit to one connection
        elif numAttachedStreams == 1:
            return BULKIO.BUSY
        else:
            return BULKIO.ACTIVE

    def _get_attachedSRIs(self):
        sris = []
        self.sri_query_lock.acquire()
        try:
            for entry in self.sriDict:
                # First value of sriDict entry is the StreamSRI object
                sris.append(copy.deepcopy(self.sriDict[entry][0]))
        finally:
            self.sri_query_lock.release()
        return sris

    def _get_usageState(self):
        self.port_lock.acquire()
        try:
            numAttachedStreams = len(self._attachedStreams.values())
        finally:
            self.port_lock.release()
        if numAttachedStreams == 0:
            return self.interface.IDLE
        # default behavior is to limit to one connection
        elif numAttachedStreams == 1:
            return self.interface.BUSY
        else:
            return self.interface.ACTIVE

    def _get_attachedStreams(self):
        return [x[0] for x in self._attachedStreams.values()]

    def _get_attachmentIds(self):
        return self._attachedStreams.keys()

    def attach(self, streamDef, userid):

        if self.logger:
            self.logger.trace("bulkio::InAttachablePort attach ENTER  (port=" + str(self.name) +")" )
            self.logger.debug("InAttachablePort.attach() - ATTACH REQUEST, STREAM/USER"  + str(streamDef) + '/' + str(userid))

        attachId = None
        self.port_lock.acquire()
        try:
            try:
                if self.logger:
                    self.logger.debug("InAttachablePort.attach() - CALLING ATTACH CALLBACK, STREAM/USER"  + str(streamDef) + '/' + str(userid) )
                if self._attach_cb != None:
                    attachId = self._attach_cb(streamDef, userid)
            except Exception, e:
                if self.logger:
                    self.logger.error("InAttachablePort.attach() - ATTACH CALLBACK EXCEPTION : " + str(e) + " STREAM/USER"  + str(streamDef) + '/' + str(userid) )
                raise self.interface.AttachError(str(e))

            if attachId == None:
                attachId = str(uuid.uuid4())

            self._attachedStreams[attachId] = (streamDef, userid)

        finally:
            self.port_lock.release()

        if self.logger:
            self.logger.debug("InAttachablePort.attach() - ATTACH COMPLETED,  ID:" + str(attachId) + " STREAM/USER: " + str(streamDef) + '/' + str(userid))
            self.logger.trace("bulkio::InAttachablePort attach EXIT (port=" + str(self.name) +")" )

        return attachId

    def detach(self, attachId):

        if self.logger:
            self.logger.trace("bulkio::InAttachablePort detach ENTER (port=" + str(self.name) +")" )
            self.logger.debug("InAttachablePort.detach() - DETACH REQUESTED, ID:" + str(attachId) )

        self.port_lock.acquire()
        try:
            if not self._attachedStreams.has_key(attachId):

                if self.logger:
                    self.logger.debug("InAttachablePort.detach() - DETACH UNKNOWN ID:" + str(attachId) )

                if attachId:
                    raise self.interface.DetachError("Stream %s not attached" % str(attachId))
                else:
                    raise self.interface.DetachError("Cannot detach Unkown ID")

            attachedStreamDef, refcnf = self._attachedStreams[attachId]

            #
            # Deallocate capacity here if applicable
            #
            try:
                if self.logger:
                    self.logger.debug("InAttachablePort.detach() - CALLING DETACH CALLBACK, ID:" + str(attachId) )

                if self._detach_cb != None:
                    self._detach_cb(attachId)
            except Exception, e:
                if self.logger:
                    self.logger.error("InAttachablePort.detach() - DETACH CALLBACK EXCEPTION: " + str(e) )
                raise self.interface.DetachError(str(e))

            # Remove the attachment from our list
            del self._attachedStreams[attachId]

        finally:
            self.port_lock.release()

        if self.logger:
            self.logger.debug("InAttachablePort.detach() - DETACH SUCCESS, ID:" + str(attachId) )
            self.logger.trace("bulkio::InAttachablePort detach EXIT (port=" + str(self.name) +")" )

    def getStreamDefinition(self, attachId):
        try:
            return self._attachedStreams[attachId][0]
        except KeyError:
            raise self.interface.StreamInputError("Stream %s not attached" % attachId)

    def getUser(self, attachId):
        try:
            return self._attachedStreams[attachId][1]
        except KeyError:
            raise self.interface.StreamInputError("Stream %s not attached" % attachId)

    def _get_activeSRIs(self):
        self.sri_query_lock.acquire()
        try:
            activeSRIs = [self.sriDict[entry][0] for entry in self.sriDict]
        finally:
            self.sri_query_lock.release()
        return activeSRIs

    def pushSRI(self, H, T):

        if self.logger:
            self.logger.trace("bulkio::InAttachablePort pushSRI ENTER (port=" + str(self.name) +")" )

        self.port_lock.acquire()
        try:
            if H.streamID not in self.sriDict:
                if self.newSriCallback:
                    self.newSriCallback( H )
                # Disable querying while adding a new SRI
                self.sri_query_lock.acquire()
                try:
                    self.sriDict[H.streamID] = (copy.deepcopy(H), copy.deepcopy(T))
                finally:
                    self.sri_query_lock.release()
            else:
                cur_H, cur_T = self.sriDict[H.streamID]
                s_same = False
                if self.sri_cmp:
                    s_same = self.sri_cmp(cur_H, H)

                t_same = False
                if self.time_cmp:
                    t_same = self.time_cmp(cur_T, T)

                self.sriChanged = ( s_same == False )  or  ( t_same == False )
                if self.sriChanged and self.sriChangeCallback:
                    self.sriChangeCallback( H )
                # Disable querying while adding a new SRI
                self.sri_query_lock.acquire()
                try:
                    self.sriDict[H.streamID] = (copy.deepcopy(H), copy.deepcopy(T))
                finally:
                    self.sri_query_lock.release()

        finally:
            self.port_lock.release()

        if self.logger:
            self.logger.trace("bulkio::InAttachablePort pushSRI EXIT (port=" + str(self.name) +")" )

class InSDDSPort(BULKIO__POA.dataSDDS,InAttachablePort):
    def __init__(self, name, logger=None, attachDetachCallback=None, sriCmp=None, timeCmp=None, PortType = 'b', newSriCallback=None, sriChangeCallback=None ):
        InAttachablePort.__init__(self, name, logger, attachDetachCallback, sriCmp, timeCmp, PortType, newSriCallback, sriChangeCallback, interface=BULKIO.dataSDDS)

class InVITA49Port(BULKIO__POA.dataVITA49,InAttachablePort):
    def __init__(self, name, logger=None, attachDetachCallback=None, sriCmp=None, timeCmp=None, PortType = 'b', newSriCallback=None, sriChangeCallback=None ):
        InAttachablePort.__init__(self, name, logger, attachDetachCallback, sriCmp, timeCmp, PortType, newSriCallback, sriChangeCallback, interface=BULKIO.dataVITA49)
