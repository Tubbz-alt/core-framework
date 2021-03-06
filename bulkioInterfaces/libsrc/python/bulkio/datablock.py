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

import itertools

import bulkio.sri

def _get_drift(begin, end, xdelta):
    real = end.time - begin.time
    expected = (end.offset - begin.offset) * self._sri.xdelta
    return real - expected

def _interleaved_to_complex(values):
    real = itertools.islice(values, 0, len(values), 2)
    imag = itertools.islice(values, 1, len(values), 2)
    return [complex(re,im) for re, im in zip(real, imag)]

class SampleTimestamp(object):
    """
    Extended time stamp container.

    SampleTimestamp adds additional context to a BULKIO.PrecisionUTCTime time
    stamp. When data is read from an sample-oriented input stream, it may span
    more than one packet, or its start may not be on a packet boundary. In
    these cases, the offset and synthetic attributes allow more sophisticated
    handling of time information.

    The offset indicates at which sample the time applies. If the sample data
    is complex, the offset should be interpreted in terms of complex samples
    (i.e., two real values per index).

    A SampleTimestamp is considered synthetic if it was generated by an input
    stream because there was no received time stamp available at that sample
    offset. This occurs when the prior read did not end on a packet boundary;
    only the first time stamp in a DataBlock can be synthetic.

    Attributes:
        time:      The time at which the referenced sample was created.
        offset:    The 0-based index of the sample at which time applies.
        synthetic: Indicates whether a time was interpolated.
    """
    __slots__ = ('time', 'offset', 'synthetic')
    def __init__(self, time, offset=0, synthetic=False):
        """
        Constructs a SampleTimestamp.

        Args:
            time:       Time stamp.
            offset:     Integral sample offset.
            synthetic:  False if time was received, True if interpolated.
        """
        self.time = time
        self.offset = offset
        self.synthetic = synthetic

class DataBlock(object):
    """
    Container for sample data and stream metadata read from an input stream.

    DataBlock encapsulates the result of a read operation on an input stream.
    It contains both data, which varies with the input stream type, and
    metadata, including signal-related information (SRI).

    While it is possible to create DataBlocks in user code, they are usually
    obtained by reading from an input stream.

    See Also:
        InputStream.read()
        InputStream.tryread()
    """
    __slots__ = ('_sri', '_data', '_sriChangeFlags', '_inputQueueFlushed', '_timestamps')
    def __init__(self, sri, data, sriChangeFlags, inputQueueFlushed):
        self._sri = sri
        self._data = data
        self._timestamps = []
        self._sriChangeFlags = sriChangeFlags
        self._inputQueueFlushed = inputQueueFlushed

    @property
    def sri(self):
        """
        BULKIO.StreamSRI: Stream metadata at the time the block was read.
        """
        return self._sri

    @property
    def buffer(self):
        """
        The data read from the stream.

        The data type varies depending on the input stream.
        """
        return self._data

    @property
    def xdelta(self):
        """
        float: The distance between two adjacent samples in the X direction.

        Because the X-axis is commonly in terms of time (that is, sri.xunits is
        BULKIO.UNITS_TIME), this is typically the reciprocal of the sample rate.
        """
        return self.sri.xdelta
    
    @property
    def sriChanged(self):
        """
        bool: Indicates whether the SRI has changed since the last read from
        the same stream.

        See Also:
            DataBlock.sriChangeFlags
        """
        return self.sriChangeFlags != bulkio.sri.NONE

    @property
    def sriChangeFlags(self):
        """
        int: Bit mask representing which SRI fields have changed since the last
        read from the same stream.

        If no SRI change has occurred since the last read, the value is
        bulkio.sri.NONE (equal to 0). Otherwise, the value is the bitwise OR of
        one or more of the following flags:
            * bulkio.sri.HVERSION
            * bulkio.sri.XSTART
            * bulkio.sri.XDELTA
            * bulkio.sri.XUNITS
            * bulkio.sri.SUBSIZE
            * bulkio.sri.YSTART
            * bulkio.sri.YDELTA
            * bulkio.sri.YUNITS
            * bulkio.sri.MODE
            * bulkio.sri.STREAMID
            * bulkio.sri.BLOCKING
            * bulkio.sri.KEYWORDS

        The HVERSION and STREAMID flags are not set in normal operation.
        """
        return self._sriChangeFlags

    @property
    def inputQueueFlushed(self):
        """
        bool: Indicates whether an input queue flush occurred.

        An input queue flush indicates that the input port was unable to keep
        up with incoming packets for non-blocking streams and emptied the queue
        to catch up.

        The input port reports a flush once, on the next queued packet. This is
        typically reflected in the next DataBlock read from any input stream
        associated with the port; however, this does not necessarily mean that
        any packets for that stream were discarded.
        """
        return self._inputQueueFlushed

    def getStartTime(self):
        """
        Gets the time stamp for the first sample.

        Returns:
            BULKIO.PrecisionUTCTime: The first time stamp.

        Raises:
            IndexError: If there are no time stamps.
        """
        self._validateTimestamps()
        return self._timestamps[0].time

    def addTimestamp(self, timestamp, offset=0, synthetic=False):
        self._timestamps.append(SampleTimestamp(timestamp, offset, synthetic))

    def getTimestamps(self):
        """
        Gets the time stamps for the sample data.

        If complex is True, the offsets of the returned time stamps should be
        interpreted in terms of complex samples.

        Valid DataBlocks obtained by reading from an input stream are
        guaranteed to have at least one time stamp, at offset 0. If the read
        spanned more than one packet, each packet's time stamp is included with
        the packet's respective offset from the first sample.

        When the DataBlock is read from an input stream, only the first time
        stamp may be synthetic. This occurs when the prior read did not consume
        a full packet worth of data. In this case, the input stream linearly
        interpolates the time stamp based on the stream's xdelta value.

        Returns:
            list(SampleTimestamp): The time stamps for the sample data.
        """
        return self._timestamps

    def getNetTimeDrift(self):
        """
        Calculates the difference between the expected and actual value of the
        last time stamp.

        If this DataBlock contains more than one time stamp, this method
        compares the last time stamp to a linearly interpolated value based on
        the initial time stamp, the StreamSRI xdelta, and the sample offset.
        This difference gives a rough estimate of the deviation between the
        nominal and actual sample rates over the sample period.

        Note:
            If the SRI X-axis is not in units of time, this value has no
            meaning.

        Returns:
            float: Difference, in seconds, between expected and actual value.
        """
        self._validateTimestamps()
        return _get_drift(self._timestamps[0], self._timestamps[-1], self.xdelta)

    def getMaxTimeDrift(self):
        """
        Calculates the largest difference between expected and actual time
        stamps in the block.

        If this DataBlock contains more than one time stamp, this method
        compares each time stamp to its linearly interpolated equivalent time
        stamp, based on the initial time stamp, the StreamSRI xdelta, and the
        sample offset. The greatest deviation is reported; this difference
        gives a rough indication of how severely the actual sample rate
        deviates from the nominal sample rate on a packet-to-packet basis.

        Note:
            If the SRI X-axis is not in units of time, this value has no
            meaning.

        Returns:
            float: Difference, in seconds, between expected and actual value.
        """
        self._validateTimestamps()
        max_drift = 0.0
        for index in xrange(1, len(self._timestamps)):
            drift = _get_drift(self._timestamps[index-1], self._timestamps[index], self.xdelta)
            if abs(drift) > abs(max):
                max_drift = drift
        return max_drift

    def _validateTimestamps(self):
        if not self._timestamps:
            raise Exception('block contains no timestamps')
        elif self._timestamps[0].offset != 0:
            raise Exception('no timestamp at offset 0')


class SampleDataBlock(DataBlock):
    """
    Extended container for sample data types.

    SampleDataBlock provides additional methods for accessing the stored data
    as either real or complex samples.

    Real vs. Complex Samples:
    Because BulkIO streams support both real and complex sample data, blocks
    store data internally as an array of real samples, and provide methods that
    allow the user to interpret the data as either real or complex.  When the
    complex mode changes, this is typically indicated with the corresponding
    SRI change flag (see sriChangeFlags). On a per-block basis, the complex
    attribute indicates whether the sample data is intended to be handled as
    real or complex:

        if block.complex:
            for value in block.cxdata:
                ...
        else:
            for value in block.data:
                ...
    """
    @property
    def data(self):
        """
        list: Sample data interpreted as Python numbers.

        The type of each element depends on the input stream. Integer types
        return int or long values, while floating point types return float
        values.

        To intepret the data as complex samples, use cxdata.
        """
        return self._data

    @property
    def size(self):
        """
        int: The size of the data in terms of real samples.
        """
        return len(self._data)

    @property
    def complex(self):
        """
        bool: Indicates whether data should be interpreted as complex samples.

        The sample data is considered complex if sri.mode is non-zero.

        If the data is complex, the offsets for the time stamps returned by
        getTimestamps() are in terms of complex samples.
        """
        return self.sri.mode != 0

    @property
    def cxdata(self):
        """
        list(complex): Sample data interpreted as Python complex values.

        To interpret the data as real samples, use data.
        """
        return _interleaved_to_complex(self.data)

    @property
    def cxsize(self):
        """
        int: The size of the data in terms of complex samples.
        """
        return self.size / 2
