# cython: language_level=3, boundscheck=False, wraparound=False, cdivision=True
"""Typed Cython prototype of the BRDF parse loop (ntriples output path).

Scope of the prototype:
  - format v1 (UTF-16BE) and v2 (UTF-8 only; other charsets -> fall back
    to the pure-Python implementation)
  - whole input pre-loaded in one bytes object (an mmap-backed production
    version would keep the same pointer-based core)
  - emits rows of pre-serialized N-Triples strings; values referenced via
    VALUE_DECL are serialized once at declaration time
"""

from cpython.unicode cimport PyUnicode_DecodeUTF8

cdef extern from "Python.h":
    object PyUnicode_DecodeUTF16(const char *s, Py_ssize_t size,
                                 const char *errors, int *byteorder)

from brdf2csv import BRDFError, _escape_literal

DEF XSD_STRING = "http://www.w3.org/2001/XMLSchema#string"


cdef class FastBRDF:
    cdef const unsigned char* buf
    cdef Py_ssize_t pos
    cdef Py_ssize_t size
    cdef object data            # keeps the bytes object alive
    cdef int version
    cdef bint done
    cdef dict declared          # id -> serialized str
    cdef public dict namespaces
    cdef public list comments

    def __init__(self, bytes data):
        self.data = data
        self.buf = data
        self.size = len(data)
        self.pos = 0
        self.done = False
        self.declared = {}
        self.namespaces = {}
        self.comments = []

        if self.size < 8 or data[:4] != b"BRDF":
            raise BRDFError("Not a Binary RDF document (bad magic number)")
        self.pos = 4
        self.version = self._int32()
        if self.version == 2:
            charset = self._string()
            if charset.lower().replace("-", "") not in ("utf8",):
                raise NotImplementedError(
                    "fast path supports UTF-8 only; got %s" % charset)
        elif self.version != 1:
            raise BRDFError("Incompatible format version: %d" % self.version)

    cdef inline void _need(self, Py_ssize_t n) except *:
        if self.pos + n > self.size:
            raise BRDFError("Unexpected end of stream")

    cdef inline int _int32(self) except? -2:
        self._need(4)
        cdef const unsigned char* b = self.buf + self.pos
        self.pos += 4
        return <int>((<unsigned int>b[0] << 24) | (<unsigned int>b[1] << 16)
                     | (<unsigned int>b[2] << 8) | <unsigned int>b[3])

    cdef inline Py_ssize_t _varint(self) except -1:
        self._need(1)
        cdef unsigned char b = self.buf[self.pos]
        self.pos += 1
        cdef Py_ssize_t v = b & 0x7F
        cdef int shift = 7
        while b & 0x80:
            self._need(1)
            b = self.buf[self.pos]
            self.pos += 1
            v |= (<Py_ssize_t>(b & 0x7F)) << shift
            shift += 7
        return v

    cdef inline Py_ssize_t _id(self) except -1:
        if self.version == 1:
            return self._int32()
        return self._varint()

    cdef unicode _string(self):
        cdef Py_ssize_t n, nb
        cdef int bo = 1  # big-endian
        if self.version == 1:
            n = self._int32()
            if n < 0:
                raise BRDFError("Negative string length")
            nb = n << 1
            self._need(nb)
            s = PyUnicode_DecodeUTF16(
                <const char*>self.buf + self.pos, nb, NULL, &bo)
            self.pos += nb
            return s
        n = self._varint()
        self._need(n)
        s = PyUnicode_DecodeUTF8(<const char*>self.buf + self.pos, n, NULL)
        self.pos += n
        return s

    cdef unicode _literal(self, unicode label, unicode lang, unicode dt):
        cdef unicode out = '"' + _escape_literal(label) + '"'
        if lang is not None:
            return out + "@" + lang
        if dt is not None and dt != XSD_STRING:
            return out + "^^<" + dt + ">"
        return out

    cdef unicode _value(self):
        """Read one value and return its N-Triples string (None = NULL)."""
        self._need(1)
        cdef unsigned char vt = self.buf[self.pos]
        self.pos += 1
        if vt == 6:                                   # VALUE_REF
            try:
                return <unicode>self.declared[self._id()]
            except KeyError:
                raise BRDFError("Reference to undeclared value id")
        if vt == 1:                                   # URI
            return "<" + self._string() + ">"
        if vt == 3:                                   # plain literal
            return self._literal(self._string(), None, None)
        if vt == 5:                                   # datatyped literal
            return self._literal(self._string(), None, self._string())
        if vt == 4:                                   # lang literal
            label = self._string()
            return self._literal(label, self._string(), None)
        if vt == 2:                                   # bnode
            return "_:" + self._string()
        if vt == 0:                                   # NULL
            return None
        if vt == 7:                                   # RDF-star triple
            s = self._value(); p = self._value(); o = self._value()
            if s is None or p is None or o is None:
                raise BRDFError("Invalid RDF-star triple value")
            return "<< " + s + " " + p + " " + o + " >>"
        raise BRDFError("Unknown value type: %d" % vt)

    cpdef list next_rows(self, Py_ssize_t max_rows):
        """Return up to max_rows (s, p, o, g) string tuples; [] at end."""
        cdef list out = []
        cdef unsigned char rt
        cdef unicode s, p, o, c
        if self.done:
            return out
        while True:
            self._need(1)
            rt = self.buf[self.pos]
            self.pos += 1
            if rt == 1:                               # STATEMENT
                s = self._value()
                p = self._value()
                o = self._value()
                c = self._value()
                if s is None or p is None or o is None:
                    raise BRDFError("Invalid statement term: null")
                out.append((s, p, o, c if c is not None else ""))
                if len(out) >= max_rows:
                    return out
            elif rt == 3:                             # VALUE_DECL
                # NB: id must be read before the value; a combined
                # `declared[self._id()] = self._value()` evaluates the
                # RHS first and corrupts the stream position.
                decl_id = self._id()
                self.declared[decl_id] = self._value()
            elif rt == 127:                           # END_OF_DATA
                self.done = True
                return out
            elif rt == 0:                             # NAMESPACE_DECL
                prefix = self._string()
                self.namespaces[prefix] = self._string()
            elif rt == 2:                             # COMMENT
                self.comments.append(self._string())
            else:
                raise BRDFError("Invalid record type: %d" % rt)
