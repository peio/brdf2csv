#!/usr/bin/env python3
"""
brdf2csv - Convert RDF4J Binary RDF (BRDF) files to CSV.

Supports both format version 1 (int32-prefixed UTF-16BE strings, as used by
older RDF4J / GraphDB releases) and format version 2 (varint-prefixed strings
in a charset declared in the header, written by RDF4J 4.x / GraphDB 10.x).

Format reference: the authoritative sources are BinaryRDFParser.java,
BinaryRDFWriter.java and BinaryRDFConstants.java in the eclipse-rdf4j/rdf4j
repository (core/rio/binary). Summary:

    Header:  'B' 'R' 'D' 'F'  +  int32 format version (big-endian)
             (v2 only) charset name as a varint-length UTF-8 string
    Records: 1-byte record type, repeated until END_OF_DATA:
        0 NAMESPACE_DECL  prefix:string  namespace:string
        1 STATEMENT       subject:value predicate:value object:value context:value
        2 COMMENT         comment:string
        3 VALUE_DECL      id (v1: int32, v2: varint)  value
        127 END_OF_DATA
    Values:  1-byte value type:
        0 NULL            (only valid as statement context = default graph)
        1 URI             uri:string
        2 BNODE           id:string
        3 PLAIN_LITERAL   label:string
        4 LANG_LITERAL    label:string  lang:string
        5 DT_LITERAL      label:string  datatype:string
        6 VALUE_REF       id (v1: int32, v2: varint) -> previously declared value
        7 TRIPLE          subject:value predicate:value object:value  (RDF-star)
    Strings: v1: int32 length in UTF-16 code units, then length*2 bytes UTF-16BE
             v2: varint length in bytes, then bytes in the declared charset
    Varints: unsigned LEB128 (7 bits per byte, little-endian groups,
             high bit = continuation)

Usage examples:
    python3 brdf2csv.py export.brf -o export.csv
    python3 brdf2csv.py export.brf.gz --format wide -o export.csv
    curl -s -H 'Accept: application/x-binary-rdf' \
        'http://localhost:7200/repositories/myrepo/statements' \
        | python3 brdf2csv.py - > export.csv

Stdlib only. Author: generated for Peio (Graphwise), 2026.
"""

import argparse
import csv
import gzip
import io
import re
import struct
import sys
from typing import BinaryIO, Dict, Iterator, List, Optional, TextIO, Tuple

MAGIC = b"BRDF"

# Record types
NAMESPACE_DECL = 0
STATEMENT = 1
COMMENT = 2
VALUE_DECL = 3
END_OF_DATA = 127

# Value types
NULL_VALUE = 0
URI_VALUE = 1
BNODE_VALUE = 2
PLAIN_LITERAL_VALUE = 3
LANG_LITERAL_VALUE = 4
DATATYPE_LITERAL_VALUE = 5
VALUE_REF = 6
TRIPLE_VALUE = 7

XSD_STRING = "http://www.w3.org/2001/XMLSchema#string"

_INT32 = struct.Struct(">i")


# ---------------------------------------------------------------------------
# Term model
# ---------------------------------------------------------------------------
# Terms are plain tuples (cheap, hashable, streaming-friendly):
#   ('uri',     iri)
#   ('bnode',   id)
#   ('literal', label, lang_or_None, datatype_or_None)
#   ('triple',  subject_term, predicate_term, object_term)   # RDF-star
#   None                                                     # default graph


class BRDFError(Exception):
    """Raised on malformed Binary RDF input."""


class BRDFParser:
    """Streaming parser for RDF4J Binary RDF, format versions 1 and 2.

    Iterate over `statements(stream)` to receive (s, p, o, c) tuples of
    terms. Namespace declarations and comments encountered so far are
    accumulated on `self.namespaces` (prefix -> namespace) and
    `self.comments`.

    Performance notes: input is consumed through an in-memory chunk buffer
    (1 MiB reads) and the record/value hot path is inlined into a single
    loop with local-variable state, avoiding per-byte stream reads and
    per-value method dispatch. Referenced values are shared tuple objects.
    """

    CHUNK = 1 << 20  # 1 MiB refill size

    def __init__(self):
        self.format_version = None  # type: Optional[int]
        self.charset = "utf-8"      # v2 only; declared in header
        self.namespaces = {}        # type: Dict[str, str]
        self.comments = []          # type: List[str]
        self._declared = {}         # type: Dict[int, tuple]

    def statements(self, stream):
        # type: (BinaryIO) -> Iterator[Tuple[tuple, tuple, tuple, Optional[tuple]]]
        """Yield (subject, predicate, object, context) term tuples."""
        read = stream.read
        data = b""
        pos = 0
        dlen = 0
        eof = False
        CHUNK = self.CHUNK
        unpack_i32 = _INT32.unpack_from
        declared = self._declared
        namespaces = self.namespaces
        comments = self.comments

        def more(min_bytes):
            """Refill the buffer so >= min_bytes are available from pos."""
            nonlocal data, pos, dlen, eof
            parts = [data[pos:]]
            have = dlen - pos
            while have < min_bytes and not eof:
                chunk = read(CHUNK if CHUNK > min_bytes - have else min_bytes - have)
                if not chunk:
                    eof = True
                    break
                parts.append(chunk)
                have += len(chunk)
            data = b"".join(parts)
            dlen = len(data)
            pos = 0
            if dlen < min_bytes:
                raise BRDFError(
                    "Unexpected end of stream (wanted %d bytes, got %d)"
                    % (min_bytes, dlen)
                )

        # ---- header (cold path) ----
        if dlen - pos < 8:
            more(8)
        if data[pos:pos + 4] != MAGIC:
            raise BRDFError("Not a Binary RDF document (bad magic number)")
        pos += 4
        self.format_version = unpack_i32(data, pos)[0]
        pos += 4
        v1 = self.format_version == 1
        if self.format_version == 2:
            # charset name: varint byte length + bytes (ASCII in practice;
            # RDF4J's own parser decodes it with the default UTF-8)
            if pos >= dlen:
                more(1)
            b = data[pos]; pos += 1
            n = b & 0x7F; shift = 7
            while b & 0x80:
                if pos >= dlen:
                    more(1)
                b = data[pos]; pos += 1
                n |= (b & 0x7F) << shift; shift += 7
            if dlen - pos < n:
                more(n)
            self.charset = data[pos:pos + n].decode("utf-8")
            pos += n
        elif not v1:
            raise BRDFError(
                "Incompatible format version: %s (supported: 1, 2)"
                % self.format_version
            )
        charset = self.charset

        # ---- inlined hot-path readers ----
        def read_string():
            nonlocal data, pos, dlen
            if v1:
                if dlen - pos < 4:
                    more(4)
                n = unpack_i32(data, pos)[0]
                pos += 4
                if n < 0:
                    raise BRDFError("Negative string length: %d" % n)
                nb = n << 1
                if dlen - pos < nb:
                    more(nb)
                s = data[pos:pos + nb].decode("utf-16-be")
                pos += nb
                return s
            # v2: varint byte length
            if pos >= dlen:
                more(1)
            b = data[pos]; pos += 1
            n = b & 0x7F; shift = 7
            while b & 0x80:
                if pos >= dlen:
                    more(1)
                b = data[pos]; pos += 1
                n |= (b & 0x7F) << shift; shift += 7
            if dlen - pos < n:
                more(n)
            s = data[pos:pos + n].decode(charset)
            pos += n
            return s

        def read_id():
            nonlocal data, pos, dlen
            if v1:
                if dlen - pos < 4:
                    more(4)
                i = unpack_i32(data, pos)[0]
                pos += 4
                return i
            if pos >= dlen:
                more(1)
            b = data[pos]; pos += 1
            i = b & 0x7F; shift = 7
            while b & 0x80:
                if pos >= dlen:
                    more(1)
                b = data[pos]; pos += 1
                i |= (b & 0x7F) << shift; shift += 7
            return i

        def read_value():
            nonlocal data, pos, dlen
            if pos >= dlen:
                more(1)
            vtype = data[pos]; pos += 1
            if vtype == 6:                       # VALUE_REF (most frequent)
                ref_id = read_id()
                try:
                    return declared[ref_id]
                except KeyError:
                    raise BRDFError(
                        "Reference to undeclared value id %d" % ref_id
                    )
            if vtype == 1:                       # URI
                return ("uri", read_string())
            if vtype == 3:                       # plain literal
                return ("literal", read_string(), None, None)
            if vtype == 5:                       # datatyped literal
                return ("literal", read_string(), None, read_string())
            if vtype == 4:                       # lang literal
                label = read_string()
                return ("literal", label, read_string(), None)
            if vtype == 2:                       # bnode
                return ("bnode", read_string())
            if vtype == 0:                       # NULL (default graph)
                return None
            if vtype == 7:                       # RDF-star triple
                subj = read_value()
                pred = read_value()
                obj = read_value()
                if subj is None or subj[0] == "literal":
                    raise BRDFError("Invalid RDF-star triple subject")
                if pred is None or pred[0] != "uri":
                    raise BRDFError("Invalid RDF-star triple predicate")
                if obj is None:
                    raise BRDFError("Invalid RDF-star triple object")
                return ("triple", subj, pred, obj)
            raise BRDFError("Unknown value type: %d" % vtype)

        # ---- record loop ----
        while True:
            if pos >= dlen:
                more(1)
            record_type = data[pos]
            pos += 1
            if record_type == STATEMENT:
                subj = read_value()
                pred = read_value()
                obj = read_value()
                ctx = read_value()
                if subj is None or subj[0] == "literal":
                    raise BRDFError("Invalid subject: %r" % (subj,))
                if pred is None or pred[0] != "uri":
                    raise BRDFError("Invalid predicate: %r" % (pred,))
                if obj is None:
                    raise BRDFError("Invalid object: null")
                if ctx is not None and ctx[0] not in ("uri", "bnode"):
                    raise BRDFError("Invalid context: %r" % (ctx,))
                yield subj, pred, obj, ctx
            elif record_type == VALUE_DECL:
                decl_id = read_id()
                # ids may be recycled by the writer; overwrite is intended
                declared[decl_id] = read_value()
            elif record_type == END_OF_DATA:
                return
            elif record_type == NAMESPACE_DECL:
                prefix = read_string()
                namespaces[prefix] = read_string()
            elif record_type == COMMENT:
                comments.append(read_string())
            else:
                raise BRDFError("Invalid record type: %d" % record_type)


# ---------------------------------------------------------------------------
# N-Triples serialization of terms
# ---------------------------------------------------------------------------

_NT_ESCAPES = {
    "\\": "\\\\",
    '"': '\\"',
    "\n": "\\n",
    "\r": "\\r",
    "\t": "\\t",
}


def _escape_literal(label: str) -> str:
    if any(c in label for c in _NT_ESCAPES):
        for raw, esc in _NT_ESCAPES.items():
            label = label.replace(raw, esc)
    return label


def term_to_ntriples(term: Optional[tuple]) -> str:
    """Serialize a term tuple in N-Triples / N-Quads (star) syntax."""
    if term is None:
        return ""  # default graph
    kind = term[0]
    if kind == "uri":
        return f"<{term[1]}>"
    if kind == "bnode":
        return f"_:{term[1]}"
    if kind == "literal":
        _, label, lang, datatype = term
        out = f'"{_escape_literal(label)}"'
        if lang is not None:
            out += f"@{lang}"
        elif datatype is not None and datatype != XSD_STRING:
            out += f"^^<{datatype}>"
        return out
    if kind == "triple":
        _, s, p, o = term
        return (
            f"<< {term_to_ntriples(s)} {term_to_ntriples(p)} "
            f"{term_to_ntriples(o)} >>"
        )
    raise ValueError(f"Unknown term kind: {kind}")


# ---------------------------------------------------------------------------
# CSV writers
# ---------------------------------------------------------------------------

NT_HEADER = ["subject", "predicate", "object", "graph"]
WIDE_HEADER = [
    "subject",
    "subject_type",
    "predicate",
    "object",
    "object_type",
    "object_lang",
    "object_datatype",
    "graph",
]


def _term_kind(term: Optional[tuple]) -> str:
    if term is None:
        return ""
    return term[0]


def _wide_lexical(term: tuple) -> str:
    """Raw lexical form for the wide format (no N-Triples decoration)."""
    kind = term[0]
    if kind in ("uri", "bnode"):
        return term[1]
    if kind == "literal":
        return term[1]
    if kind == "triple":  # nested triples only make sense serialized
        return term_to_ntriples(term)
    raise ValueError(f"Unknown term kind: {kind}")


def write_csv(
    parser,          # type: BRDFParser
    stream,          # type: BinaryIO
    out,             # type: TextIO
    fmt="ntriples",  # type: str
    delimiter=",",   # type: str
    header=True,     # type: bool
    progress_every=0,  # type: int
    limit=0,         # type: int
):
    # type: (...) -> int
    """Stream statements from `stream` to CSV on `out`. Returns row count.

    Serialized term strings are memoized: RDF4J writers emit repeated values
    as VALUE_REFs (shared tuple objects here), and URIs/predicates repeat
    heavily in real data, so most rows hit the cache. Rows are flushed with
    writerows() in batches. Memory stays bounded via a cache cap.
    """
    writer = csv.writer(out, delimiter=delimiter, quoting=csv.QUOTE_MINIMAL)
    count = 0
    memo = {}          # term tuple -> serialized string
    memo_get = memo.get
    MEMO_CAP = 1 << 20  # safety valve for pathological files
    BATCH = 8192
    batch = []
    append = batch.append
    writerows = writer.writerows

    def ser(term):
        s = memo_get(term)
        if s is None:
            s = term_to_ntriples(term)
            if len(memo) >= MEMO_CAP:
                memo.clear()
            memo[term] = s
        return s

    if fmt == "ntriples":
        if header:
            writer.writerow(NT_HEADER)
        for s, p, o, c in parser.statements(stream):
            # memoize subject/predicate/context (URIs repeat heavily) and
            # uri/bnode objects; serialize literal objects directly since
            # their labels are the mostly-unique, memory-heavy part.
            if o[0] == "literal":
                obj_s = term_to_ntriples(o)
            else:
                obj_s = ser(o)
            append((ser(s), ser(p), obj_s, ser(c) if c is not None else ""))
            count += 1
            if len(batch) >= BATCH:
                writerows(batch)
                del batch[:]
            if progress_every and count % progress_every == 0:
                print("... {:,} statements".format(count), file=sys.stderr)
            if limit and count >= limit:
                break
    elif fmt == "wide":
        if header:
            writer.writerow(WIDE_HEADER)
        for s, p, o, c in parser.statements(stream):
            lang = o[2] if o[0] == "literal" else None
            datatype = o[3] if o[0] == "literal" else None
            if o[0] == "literal" and lang is None and datatype is None:
                datatype = XSD_STRING  # RDF 1.1: plain literal == xsd:string
            append(
                (
                    _wide_lexical(s),
                    s[0],
                    p[1],
                    _wide_lexical(o),
                    o[0],
                    lang or "",
                    datatype or "",
                    _wide_lexical(c) if c is not None else "",
                )
            )
            count += 1
            if len(batch) >= BATCH:
                writerows(batch)
                del batch[:]
            if progress_every and count % progress_every == 0:
                print("... {:,} statements".format(count), file=sys.stderr)
            if limit and count >= limit:
                break
    else:
        raise ValueError("Unknown output format: %s" % fmt)

    if batch:
        writerows(batch)
    return count


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _open_input(path: str) -> BinaryIO:
    if path == "-":
        stream: BinaryIO = sys.stdin.buffer
        # gzip auto-detect on stdin via magic peek
        buffered = io.BufferedReader(stream)
        head = buffered.peek(2)[:2]
        if head == b"\x1f\x8b":
            return gzip.open(buffered)  # type: ignore[return-value]
        return buffered
    if path.endswith(".gz"):
        return gzip.open(path, "rb")  # type: ignore[return-value]
    return open(path, "rb")


def main(argv=None):
    # type: (Optional[List[str]]) -> int
    ap = argparse.ArgumentParser(
        prog="brdf2csv",
        description="Convert RDF4J Binary RDF (as exported by GraphDB) to CSV.",
    )
    ap.add_argument("input", help="Input .brf file, .brf.gz, or '-' for stdin")
    ap.add_argument(
        "-o", "--output", default="-", help="Output CSV file (default: stdout)"
    )
    ap.add_argument(
        "--format",
        choices=["ntriples", "wide"],
        default="ntriples",
        help="ntriples: 4 columns with N-Triples-encoded terms (lossless, "
        "round-trippable); wide: 8 columns with raw lexical values plus "
        "type/lang/datatype columns (analysis-friendly). Default: ntriples",
    )
    ap.add_argument("--delimiter", default=",", help="CSV delimiter (default: ,)")
    ap.add_argument(
        "--no-header", action="store_true", help="Do not write a header row"
    )
    ap.add_argument(
        "--limit",
        type=int,
        default=0,
        metavar="N",
        help="Stop after N statements (useful for previewing large exports)",
    )
    ap.add_argument(
        "--progress",
        type=int,
        nargs="?",
        const=100_000,
        default=0,
        metavar="N",
        help="Report progress to stderr every N statements (default N: 100000)",
    )
    ap.add_argument(
        "--namespaces",
        metavar="FILE",
        help="Also write namespace declarations found in the stream to FILE "
        "as a 2-column CSV (prefix,namespace)",
    )
    args = ap.parse_args(argv)

    if len(args.delimiter) != 1:
        ap.error("--delimiter must be a single character (e.g. ',' ';' '\\t')")

    parser = BRDFParser()
    instream = _open_input(args.input)
    out: TextIO
    close_out = False
    if args.output == "-":
        out = sys.stdout
    elif args.output.endswith(".gz"):
        out = gzip.open(args.output, "wt", newline="", encoding="utf-8")
        close_out = True
    else:
        out = open(args.output, "w", newline="", encoding="utf-8")
        close_out = True

    try:
        count = write_csv(
            parser,
            instream,
            out,
            fmt=args.format,
            delimiter=args.delimiter,
            header=not args.no_header,
            progress_every=args.progress,
            limit=args.limit,
        )
    except BRDFError as exc:
        print(f"brdf2csv: parse error: {exc}", file=sys.stderr)
        return 1
    except BrokenPipeError:
        return 0
    finally:
        if close_out:
            out.close()
        if instream is not sys.stdin.buffer:
            instream.close()

    if args.namespaces and parser.namespaces:
        with open(args.namespaces, "w", newline="", encoding="utf-8") as nsf:
            w = csv.writer(nsf)
            w.writerow(["prefix", "namespace"])
            for prefix, ns in sorted(parser.namespaces.items()):
                w.writerow([prefix, ns])

    print(
        f"brdf2csv: wrote {count:,} statements "
        f"(format v{parser.format_version}, "
        f"{len(parser.namespaces)} namespaces)",
        file=sys.stderr,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
