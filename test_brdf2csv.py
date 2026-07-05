#!/usr/bin/env python3
"""Tests for brdf2csv: handcrafted byte streams for format v1 and v2."""

import csv
import io
import struct
import unittest

from brdf2csv import (
    BRDFError,
    BRDFParser,
    term_to_ntriples,
    write_csv,
)

FOAF = "http://xmlns.com/foaf/0.1/"
EX = "http://example.org/"
XSD = "http://www.w3.org/2001/XMLSchema#"


# ---------------------------------------------------------------------------
# Reference encoder (mirrors BinaryRDFWriter.java for v1 and v2)
# ---------------------------------------------------------------------------

class Encoder:
    def __init__(self, version: int, charset: str = "utf-8") -> None:
        assert version in (1, 2)
        self.version = version
        self.charset = charset
        self.buf = io.BytesIO()
        self.buf.write(b"BRDF")
        self.buf.write(struct.pack(">i", version))
        if version == 2:
            self._string(charset)

    def _varint(self, value: int) -> None:
        assert value >= 0
        while value > 127:
            self.buf.write(bytes([(value & 0x7F) | 0x80]))
            value >>= 7
        self.buf.write(bytes([value]))

    def _string(self, s: str) -> None:
        if self.version == 1:
            encoded = s.encode("utf-16-be")
            # length is in UTF-16 code units = bytes // 2
            self.buf.write(struct.pack(">i", len(encoded) // 2))
            self.buf.write(encoded)
        else:
            encoded = s.encode(self.charset)
            self._varint(len(encoded))
            self.buf.write(encoded)

    def _id(self, i: int) -> None:
        if self.version == 1:
            self.buf.write(struct.pack(">i", i))
        else:
            self._varint(i)

    # value encoders --------------------------------------------------------
    def null(self) -> None:
        self.buf.write(bytes([0]))

    def uri(self, u: str) -> None:
        self.buf.write(bytes([1]))
        self._string(u)

    def bnode(self, bid: str) -> None:
        self.buf.write(bytes([2]))
        self._string(bid)

    def plain_literal(self, label: str) -> None:
        self.buf.write(bytes([3]))
        self._string(label)

    def lang_literal(self, label: str, lang: str) -> None:
        self.buf.write(bytes([4]))
        self._string(label)
        self._string(lang)

    def dt_literal(self, label: str, dt: str) -> None:
        self.buf.write(bytes([5]))
        self._string(label)
        self._string(dt)

    def value_ref(self, i: int) -> None:
        self.buf.write(bytes([6]))
        self._id(i)

    def triple_start(self) -> None:
        self.buf.write(bytes([7]))  # followed by three encoded values

    # record encoders -------------------------------------------------------
    def namespace(self, prefix: str, ns: str) -> None:
        self.buf.write(bytes([0]))
        self._string(prefix)
        self._string(ns)

    def statement_start(self) -> None:
        self.buf.write(bytes([1]))  # followed by four encoded values

    def comment(self, text: str) -> None:
        self.buf.write(bytes([2]))
        self._string(text)

    def value_decl_start(self, i: int) -> None:
        self.buf.write(bytes([3]))
        self._id(i)  # followed by one encoded value

    def end(self) -> bytes:
        self.buf.write(bytes([127]))
        return self.buf.getvalue()


def build_sample(version: int, charset: str = "utf-8") -> bytes:
    """A stream exercising every record and value type."""
    e = Encoder(version, charset)
    e.comment("test file")
    e.namespace("foaf", FOAF)
    e.namespace("ex", EX)

    # value decls: 0 -> ex:george (uri), 1 -> foaf:name
    e.value_decl_start(0)
    e.uri(EX + "george")
    e.value_decl_start(1)
    e.uri(FOAF + "name")

    # stmt 1: <ex:george> foaf:name "George" .  (default graph, via refs)
    e.statement_start()
    e.value_ref(0)
    e.value_ref(1)
    e.plain_literal("George")
    e.null()

    # stmt 2: lang literal with non-BMP char (UTF-16 surrogate pair test),
    # named graph
    e.statement_start()
    e.value_ref(0)
    e.uri(FOAF + "nick")
    e.lang_literal("Жоро \U0001F600 \"quoted\",\nline2", "bg")
    e.uri(EX + "graph1")

    # stmt 3: datatyped literal, bnode subject, bnode graph
    e.statement_start()
    e.bnode("b1")
    e.uri(EX + "age")
    e.dt_literal("42", XSD + "integer")
    e.bnode("g1")

    # id recycling: redeclare id 0
    e.value_decl_start(0)
    e.uri(EX + "anna")
    e.statement_start()
    e.value_ref(0)
    e.value_ref(1)
    e.plain_literal("Anna")
    e.null()

    # RDF-star: << ex:anna foaf:name "Anna" >> ex:certainty "0.9"^^xsd:decimal
    e.statement_start()
    e.triple_start()
    e.value_ref(0)
    e.value_ref(1)
    e.plain_literal("Anna")
    e.uri(EX + "certainty")
    e.dt_literal("0.9", XSD + "decimal")
    e.null()

    return e.end()


EXPECTED = [
    (
        ("uri", EX + "george"),
        ("uri", FOAF + "name"),
        ("literal", "George", None, None),
        None,
    ),
    (
        ("uri", EX + "george"),
        ("uri", FOAF + "nick"),
        ("literal", 'Жоро \U0001F600 "quoted",\nline2', "bg", None),
        ("uri", EX + "graph1"),
    ),
    (
        ("bnode", "b1"),
        ("uri", EX + "age"),
        ("literal", "42", None, XSD + "integer"),
        ("bnode", "g1"),
    ),
    (
        ("uri", EX + "anna"),
        ("uri", FOAF + "name"),
        ("literal", "Anna", None, None),
        None,
    ),
    (
        (
            "triple",
            ("uri", EX + "anna"),
            ("uri", FOAF + "name"),
            ("literal", "Anna", None, None),
        ),
        ("uri", EX + "certainty"),
        ("literal", "0.9", None, XSD + "decimal"),
        None,
    ),
]


class ParserTests(unittest.TestCase):
    def _parse_all(self, data: bytes):
        p = BRDFParser()
        stmts = list(p.statements(io.BytesIO(data)))
        return p, stmts

    def test_v1_full(self):
        p, stmts = self._parse_all(build_sample(1))
        self.assertEqual(p.format_version, 1)
        self.assertEqual(stmts, EXPECTED)
        self.assertEqual(p.namespaces, {"foaf": FOAF, "ex": EX})
        self.assertEqual(p.comments, ["test file"])

    def test_v2_utf8(self):
        p, stmts = self._parse_all(build_sample(2, "utf-8"))
        self.assertEqual(p.format_version, 2)
        self.assertEqual(p.charset.lower(), "utf-8")
        self.assertEqual(stmts, EXPECTED)

    def test_v2_latin1_charset(self):
        # v2 with a non-default charset declared in the header. Note: the
        # charset *name* is ASCII, so it decodes identically under UTF-8,
        # matching the behavior of RDF4J's own parser.
        e = Encoder(2, "iso-8859-1")
        e.statement_start()
        e.uri(EX + "café")
        e.uri(EX + "p")
        e.plain_literal("crème brûlée")
        e.null()
        p, stmts = self._parse_all(e.end())
        self.assertEqual(p.charset, "iso-8859-1")
        self.assertEqual(stmts[0][0], ("uri", EX + "café"))
        self.assertEqual(stmts[0][2], ("literal", "crème brûlée", None, None))

    def test_varint_boundaries(self):
        # long string forcing a multi-byte varint length in v2
        e = Encoder(2)
        long_label = "x" * 300  # 300 > 127 -> 2-byte varint
        e.statement_start()
        e.uri(EX + "s")
        e.uri(EX + "p")
        e.plain_literal(long_label)
        e.null()
        _, stmts = self._parse_all(e.end())
        self.assertEqual(stmts[0][2], ("literal", long_label, None, None))

    def test_large_ref_id_varint(self):
        e = Encoder(2)
        e.value_decl_start(100000)
        e.uri(EX + "s")
        e.statement_start()
        e.value_ref(100000)
        e.uri(EX + "p")
        e.plain_literal("v")
        e.null()
        _, stmts = self._parse_all(e.end())
        self.assertEqual(stmts[0][0], ("uri", EX + "s"))

    def test_bad_magic(self):
        with self.assertRaises(BRDFError):
            self._parse_all(b"XXXX" + struct.pack(">i", 1) + bytes([127]))

    def test_bad_version(self):
        with self.assertRaises(BRDFError):
            self._parse_all(b"BRDF" + struct.pack(">i", 99) + bytes([127]))

    def test_truncated(self):
        data = build_sample(1)[:-10]
        with self.assertRaises(BRDFError):
            self._parse_all(data)

    def test_undeclared_ref(self):
        e = Encoder(1)
        e.statement_start()
        e.value_ref(5)
        with self.assertRaises(BRDFError):
            self._parse_all(e.end())

    def test_literal_subject_rejected(self):
        e = Encoder(1)
        e.statement_start()
        e.plain_literal("nope")
        e.uri(EX + "p")
        e.plain_literal("v")
        e.null()
        with self.assertRaises(BRDFError):
            self._parse_all(e.end())


class SerializationTests(unittest.TestCase):
    def test_ntriples_terms(self):
        self.assertEqual(term_to_ntriples(("uri", EX + "a")), f"<{EX}a>")
        self.assertEqual(term_to_ntriples(("bnode", "b1")), "_:b1")
        self.assertEqual(
            term_to_ntriples(("literal", 'say "hi"\n', None, None)),
            '"say \\"hi\\"\\n"',
        )
        self.assertEqual(
            term_to_ntriples(("literal", "здравей", "bg", None)),
            '"здравей"@bg',
        )
        self.assertEqual(
            term_to_ntriples(("literal", "42", None, XSD + "integer")),
            f'"42"^^<{XSD}integer>',
        )
        # xsd:string datatype is implicit in RDF 1.1 -> omitted
        self.assertEqual(
            term_to_ntriples(("literal", "s", None, XSD + "string")), '"s"'
        )
        self.assertEqual(
            term_to_ntriples(
                ("triple", ("uri", EX + "s"), ("uri", EX + "p"), ("bnode", "o"))
            ),
            f"<< <{EX}s> <{EX}p> _:o >>",
        )

    def test_csv_ntriples_format(self):
        out = io.StringIO()
        n = write_csv(BRDFParser(), io.BytesIO(build_sample(2)), out)
        self.assertEqual(n, 5)
        rows = list(csv.reader(io.StringIO(out.getvalue())))
        self.assertEqual(rows[0], ["subject", "predicate", "object", "graph"])
        self.assertEqual(
            rows[1],
            [f"<{EX}george>", f"<{FOAF}name>", '"George"', ""],
        )
        # embedded newline/quote/comma survives CSV round-trip
        self.assertIn("\\n", rows[2][2])
        self.assertEqual(rows[2][3], f"<{EX}graph1>")
        self.assertTrue(rows[5][0].startswith("<< "))

    def test_csv_wide_format(self):
        out = io.StringIO()
        n = write_csv(
            BRDFParser(), io.BytesIO(build_sample(1)), out, fmt="wide"
        )
        self.assertEqual(n, 5)
        rows = list(csv.reader(io.StringIO(out.getvalue())))
        self.assertEqual(rows[0][:3], ["subject", "subject_type", "predicate"])
        # plain literal normalized to xsd:string in wide mode
        self.assertEqual(rows[1][4], "literal")
        self.assertEqual(rows[1][6], XSD + "string")
        # lang literal
        self.assertEqual(rows[2][5], "bg")
        # bnode subject: raw id, no _: prefix in wide mode
        self.assertEqual(rows[3][0], "b1")
        self.assertEqual(rows[3][1], "bnode")

    def test_limit(self):
        out = io.StringIO()
        n = write_csv(
            BRDFParser(), io.BytesIO(build_sample(1)), out, limit=2
        )
        self.assertEqual(n, 2)


if __name__ == "__main__":
    unittest.main(verbosity=2)
