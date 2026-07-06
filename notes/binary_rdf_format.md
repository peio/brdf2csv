# The RDF4J Binary RDF Format

This document describes the structure of the [RDF4J Binary RDF](https://rdf4j.org/documentation/reference/rdf4j-binary/) format
(`application/x-binary-rdf`, `RDFFormat.BINARY`, usual file extension
`.brf`), covering both existing format versions: **version 1** (the original
format, described on the rdf4j.org documentation page) and **version 2**
(the current default written by RDF4J 4.x and the GraphDB releases built on
it).

Everything below is taken directly from the reference Java implementation in
the [`eclipse-rdf4j/rdf4j`](https://github.com/eclipse-rdf4j/rdf4j) repository, module [`core/rio/binary`](https://github.com/eclipse-rdf4j/rdf4j/tree/main/core/rio/binary):

- `BinaryRDFConstants.java` — magic number, version numbers, record and
  value type constants
- `BinaryRDFWriter.java` — serialization, statement buffering, value ID
  assignment and recycling
- `BinaryRDFParser.java` — deserialization and version handling
- `BinaryRDFWriterSettings.java` — configurable writer settings and defaults
- `org.eclipse.rdf4j.common.io.IOUtil` — the variable-length integer
  encoding used by version 2

All byte sequences shown were generated with an encoder that mirrors
`BinaryRDFWriter.java` and were verified by parsing them back; none are
hand-written.

---

## 1. Overall stream structure

A Binary RDF document is a single byte stream: a fixed header followed by a
sequence of records, terminated by an `END_OF_DATA` record.

```
+-----------------------------+
|  HEADER                     |
|    magic  "BRDF"  (4 bytes) |
|    format version  (int32)  |
|    charset name (v2 only)   |
+-----------------------------+
|  RECORD                     |   record types:
+-----------------------------+     NAMESPACE_DECL (0)
|  RECORD                     |     STATEMENT      (1)
+-----------------------------+     COMMENT        (2)
|  ...                        |     VALUE_DECL     (3)
+-----------------------------+
|  END_OF_DATA  (0x7F)        |
+-----------------------------+
```

All multi-byte integers are big-endian. There is no padding or alignment
anywhere in the format.

---

## 2. Primitive encodings

### 2.1 `int32`

A 4-byte big-endian signed integer (Java `DataOutputStream.writeInt`). Used
for the format version in both versions, and for all lengths and IDs in
version 1.

### 2.2 Variable-length integer (`varint`, version 2 only)

Version 2 replaces most `int32` fields with an unsigned variable-length
integer: the value is emitted 7 bits at a time, least-significant group
first, with the high bit of each byte set on all but the final byte. The
reference implementation (`IOUtil`):

```java
public static void writeVarInt(OutputStream out, int value) throws IOException {
    if (value < 0) {
        throw new IllegalArgumentException("Unable to write negative variable length integer");
    }
    while (value > 127) {
        out.write(value & 0b01111111 | 0b10000000);
        value >>>= 7;
    }
    out.write(value);
}

public static int readVarInt(InputStream in) throws IOException {
    byte b = readByte(in);
    int v = b & 0b01111111;
    for (int i = 7; (b & 0b10000000) != 0; i += 7) {
        b = readByte(in);
        v |= (b & 0b01111111) << i;
    }
    return v;
}
```

Negative values are not representable. Worked encodings:

| Value | Encoded bytes | Size |
|---:|---|---:|
| 0 | `00` | 1 |
| 1 | `01` | 1 |
| 127 | `7F` | 1 |
| 128 | `80 01` | 2 |
| 129 | `81 01` | 2 |
| 300 | `AC 02` | 2 |
| 16383 | `FF 7F` | 2 |
| 16384 | `80 80 01` | 3 |
| 2147483647 | `FF FF FF FF 07` | 5 |

Since values below 128 occupy a single byte, the string lengths and value
IDs that dominate real files are almost always one byte in version 2, versus
four in version 1.

### 2.3 Strings

Strings differ between the versions in both the meaning of the length prefix
and the character encoding (`BinaryRDFWriter.writeString`):

| | Version 1 | Version 2 |
|---|---|---|
| Length prefix | `int32` | `varint` |
| Length counts | **UTF-16 code units** (Java `String.length()`) | **bytes** |
| Payload encoding | UTF-16BE (`V1_STRING_CHARSET`) | the charset declared in the header (UTF-8 by default) |
| Payload size | length × 2 bytes | length bytes |

Examples (length prefix in **bold** conceptually; shown as leading bytes):

| String | Version 1 bytes | Version 2 (UTF-8) bytes |
|---|---|---|
| `foaf` | `00 00 00 04  00 66 00 6F 00 61 00 66` | `04  66 6F 61 66` |
| `café` | `00 00 00 04  00 63 00 61 00 66 00 E9` | `05  63 61 66 C3 A9` |
| `😀` (U+1F600) | `00 00 00 02  D8 3D DE 00` | `04  F0 9F 98 80` |

The last row shows the code-unit semantics of version 1: the single
character U+1F600 lies outside the Basic Multilingual Plane, so it is a
UTF-16 *surrogate pair* — length prefix 2, payload `D8 3D DE 00` (high
surrogate `D83D`, low surrogate `DE00`). In version 2 the same character is
four UTF-8 bytes with length prefix 4.

---

## 3. Header

### 3.1 Version 1

| Offset | Size | Field | Value |
|---:|---:|---|---|
| 0 | 4 | Magic number | `42 52 44 46` (`"BRDF"`) |
| 4 | 4 | Format version (`int32`) | `00 00 00 01` |

Complete version 1 header: `42 52 44 46 00 00 00 01`

### 3.2 Version 2

| Offset | Size | Field | Value |
|---:|---:|---|---|
| 0 | 4 | Magic number | `42 52 44 46` (`"BRDF"`) |
| 4 | 4 | Format version (`int32`) | `00 00 00 02` |
| 8 | var | Charset name: varint byte length + name bytes | e.g. `05 75 74 66 2D 38` (`"utf-8"`) |

Complete default version 2 header:
`42 52 44 46 00 00 00 02 05 75 74 66 2D 38`

Two implementation details of the charset field, exactly as coded:

- The writer encodes the charset *name* in the charset itself:
  `charset.toString().getBytes(charset)` (`BinaryRDFWriter.startRDF`).
- The parser decodes the name with its initial default charset, UTF-8
  (`BinaryRDFParser`: `private Charset charset = StandardCharsets.UTF_8;`
  then `charset = Charset.forName(readString());`).

The parser is explicitly backward-compatible: it accepts version 1 and
version 2 and reports a fatal error (`"Incompatible format version"`) for
anything else. Note that the format version is a plain `int32` in *both*
versions — a reader can always read the first 8 bytes the same way and then
branch.

---

## 4. Records

After the header, the stream is a sequence of records. Each record begins
with a single type byte (`BinaryRDFConstants`):

| Byte | Constant | Payload |
|---:|---|---|
| 0 | `NAMESPACE_DECL` | prefix : string, namespace IRI : string |
| 1 | `STATEMENT` | subject : value, predicate : value, object : value, context : value |
| 2 | `COMMENT` | comment text : string |
| 3 | `VALUE_DECL` | id : int32 (v1) / varint (v2), value : value |
| 127 | `END_OF_DATA` | none; terminates the document |

(The constants file also contains a commented-out `ERROR = 126` entry; no
such record is written or parsed.)

### 4.1 `NAMESPACE_DECL` (0)

```
+------+----------------+-------------------------+
| 0x00 | prefix: string | namespace IRI: string   |
+------+----------------+-------------------------+
```

Example (version 2), declaring `foaf:` → `http://xmlns.com/foaf/0.1/`:

```
00                                               NAMESPACE_DECL
04 66 6F 61 66                                   "foaf"
1A 68 74 74 70 3A 2F 2F 78 6D 6C 6E 73 2E 63 6F  len 26, "http://xmlns.com/foaf/0.1/"
6D 2F 66 6F 61 66 2F 30 2E 31 2F
```

### 4.2 `STATEMENT` (1)

```
+------+---------+-----------+--------+---------+
| 0x01 | subject | predicate | object | context |
+------+---------+-----------+--------+---------+
         value      value      value    value
```

Each of the four positions holds one *value* (section 5). The context
position uses the `NULL` value (type byte `0x00`) for statements in the
default graph; `NULL` is written by the writer only in the context position
(`writeValueOrId` emits `NULL_VALUE` when the context is `null`).

### 4.3 `COMMENT` (2)

```
+------+----------------------+
| 0x02 | comment text: string |
+------+----------------------+
```

### 4.4 `VALUE_DECL` (3)

```
+------+---------------------------+--------+
| 0x03 | id: int32 (v1)/varint (v2)| value  |
+------+---------------------------+--------+
```

Associates a numeric ID with a value so that later occurrences can be
written as a `VALUE_REF`. Section 6 describes exactly when the writer emits
these and how IDs are assigned and recycled.

### 4.5 `END_OF_DATA` (127)

The single byte `7F`. Written by `endRDF()` after flushing any queued
statements.

---

## 5. Values

A value begins with a single type byte:

| Byte | Constant | Payload |
|---:|---|---|
| 0 | `NULL_VALUE` | none (default-graph context) |
| 1 | `URI_VALUE` | IRI : string |
| 2 | `BNODE_VALUE` | blank node ID : string |
| 3 | `PLAIN_LITERAL_VALUE` | label : string |
| 4 | `LANG_LITERAL_VALUE` | label : string, language tag : string |
| 5 | `DATATYPE_LITERAL_VALUE` | label : string, datatype IRI : string |
| 6 | `VALUE_REF` | id : int32 (v1) / varint (v2) |
| 7 | `TRIPLE_VALUE` | subject : value, predicate : value, object : value |

Literal selection follows `BinaryRDFWriter.writeLiteral` exactly: a literal
with a language tag is written as `LANG_LITERAL_VALUE`; otherwise, a literal
whose datatype equals `xsd:string` is written as `PLAIN_LITERAL_VALUE`
(the datatype is implied, per RDF 1.1); any other datatype is written as
`DATATYPE_LITERAL_VALUE` with the datatype IRI spelled out in full.

Example — the literal `"42"^^<http://www.w3.org/2001/XMLSchema#integer>`
(version 2):

```
05                                               DATATYPE_LITERAL_VALUE
02 34 32                                         len 2, "42"
28 68 74 74 70 3A 2F 2F 77 77 77 2E 77 33 2E 6F  len 40,
72 67 2F 32 30 30 31 2F 58 4D 4C 53 63 68 65 6D  "http://www.w3.org/2001/
61 23 69 6E 74 65 67 65 72                        XMLSchema#integer"
```

`TRIPLE_VALUE` carries an RDF-star quoted triple: three nested values follow
recursively (`writeTripleTerm` calls `writeValue` for subject, predicate and
object — note that nested values are always written inline, not as
references). Example — the quoted triple `<< _:b1 <http://example.org/p>
"x" >>` (version 2):

```
07                                               TRIPLE_VALUE
02 02 62 31                                      BNODE_VALUE, len 2, "b1"
01 14 68 74 74 70 3A 2F 2F 65 78 61 6D 70 6C 65  URI_VALUE, len 20,
2E 6F 72 67 2F 70                                "http://example.org/p"
03 01 78                                         PLAIN_LITERAL_VALUE, len 1, "x"
```

---

## 6. Value declarations, references, and ID recycling

This is the mechanism that makes the format compact, and it is driven
entirely by the writer's statement buffer (`BinaryRDFWriter`):

1. Incoming statements are queued rather than written immediately. The
   queue holds up to `BUFFER_SIZE` statements (default **8192**). Writing
   begins when the queue is full, and `endRDF()` drains whatever remains.
2. For every subject, predicate, object and context entering the queue, the
   writer counts occurrences (`incValueFreq`). The moment a value's count
   reaches **2**, an ID is assigned and a `VALUE_DECL` record is emitted
   immediately (`assignId`) — that is, starting from its second occurrence
   within the buffer window, the value will be written as an ID.
3. When a statement is written, each position is emitted either as a
   `VALUE_REF` (if the value has an ID) or inline (`writeValueOrId`). The
   occurrence count is decremented as positions are written.
4. When a value's count reaches zero: if it never received an ID it is
   simply forgotten; if it has an ID and ID recycling is enabled, the value
   is forgotten and its ID returns to a pool (`idPool`) for reuse by a
   future `VALUE_DECL`. With recycling disabled, the value keeps its ID for
   the remainder of the document.

Consequences for readers, following `BinaryRDFParser`:

- A `VALUE_DECL` may reuse an ID seen earlier in the stream; the new
  declaration simply replaces the old binding from that point on.
- A `VALUE_REF` always refers to the most recent `VALUE_DECL` with that ID.
- IDs are small non-negative integers allocated sequentially from 0
  (`nextId`), so in version 2 they are almost always single-byte varints.

Writer settings (`BinaryRDFWriterSettings`), all applied per writer:

| Setting key | Meaning | Default |
|---|---|---|
| `org.eclipse.rdf4j.rio.binary.format_version` | Format version to write | **2** |
| `org.eclipse.rdf4j.rio.binary.buffer_size` | Statement buffer window for duplicate detection | **8192** |
| `org.eclipse.rdf4j.rio.binary.charset` | String charset (version 2 only) | **UTF-8** |
| `org.eclipse.rdf4j.rio.binary.recycle_ids` | Recycle IDs of exhausted values (version 2 only) | **true** |

---

## 7. Complete worked example (version 2)

The following 192-byte document declares one namespace, declares one shared
value, and contains two statements:

```turtle
@prefix foaf: <http://xmlns.com/foaf/0.1/> .
<http://example.org/george> foaf:name "George" .                             # default graph
<http://example.org/george> foaf:mbox "Жорж"@bg <http://example.org/g1> .    # named graph
```

Annotated dump (offsets in decimal):

```
off  bytes                                            meaning
---  -----------------------------------------------  ------------------------------------
  0  42 52 44 46                                      magic "BRDF"
  4  00 00 00 02                                      format version = 2 (int32)
  8  05 75 74 66 2D 38                                charset: len 5, "utf-8"

 14  00                                               NAMESPACE_DECL
 15  04 66 6F 61 66                                   prefix: len 4, "foaf"
 20  1A 68 74 74 70 3A 2F 2F 78 6D 6C 6E 73 2E 63 6F  IRI: len 26,
     6D 2F 66 6F 61 66 2F 30 2E 31 2F                 "http://xmlns.com/foaf/0.1/"

 47  03                                               VALUE_DECL
 48  00                                               id = 0 (varint)
 49  01 19 68 74 74 70 3A 2F 2F 65 78 61 6D 70 6C 65  URI_VALUE: len 25,
     2E 6F 72 67 2F 67 65 6F 72 67 65                 "http://example.org/george"

 76  01                                               STATEMENT
 77  06 00                                            subject:   VALUE_REF id 0
 79  01 1E 68 74 74 70 3A 2F 2F 78 6D 6C 6E 73 2E 63  predicate: URI_VALUE len 30,
     6F 6D 2F 66 6F 61 66 2F 30 2E 31 2F 6E 61 6D 65  "http://xmlns.com/foaf/0.1/name"
111  03 06 47 65 6F 72 67 65                          object:    PLAIN_LITERAL len 6 "George"
119  00                                               context:   NULL (default graph)

120  01                                               STATEMENT
121  06 00                                            subject:   VALUE_REF id 0
123  01 1E 68 74 74 70 3A 2F 2F 78 6D 6C 6E 73 2E 63  predicate: URI_VALUE len 30,
     6F 6D 2F 66 6F 61 66 2F 30 2E 31 2F 6D 62 6F 78  "http://xmlns.com/foaf/0.1/mbox"
155  04 08 D0 96 D0 BE D1 80 D0 B6 02 62 67           object:    LANG_LITERAL len 8
                                                      "Жорж" (UTF-8), lang len 2 "bg"
168  01 15 68 74 74 70 3A 2F 2F 65 78 61 6D 70 6C 65  context:   URI_VALUE len 21,
     2E 6F 72 67 2F 67 31                             "http://example.org/g1"

191  7F                                               END_OF_DATA
```

Points worth noticing in the dump: the shared subject costs two bytes per
use (`06 00`) after its one-time 27-byte declaration; every length in this
document fits in a single varint byte; the Cyrillic label occupies 8 UTF-8
bytes with a byte-count prefix (in version 1 it would be 8 UTF-16BE bytes
with a code-unit count of 4); and the default-graph context is the single
byte `00`.

The same logical document in version 1 differs only mechanically: no charset
field in the header, every length and ID becomes a 4-byte `int32`, and every
string payload is UTF-16BE — the version 1 encoding is 366 bytes for the
identical content, versus 192 in version 2.

---

## 8. Version comparison summary

| Aspect | Version 1 | Version 2 |
|---|---|---|
| Header | magic + version | magic + version + charset name |
| Integer lengths / IDs | `int32` (4 bytes) | varint (usually 1 byte) |
| String length semantics | UTF-16 code units | bytes |
| String encoding | UTF-16BE (fixed) | declared charset, UTF-8 by default |
| Record and value type bytes | identical | identical |
| Record and value grammar | identical | identical |
| ID recycling writer setting | — | available, on by default |
| Written by | RDF4J releases before format 2 | RDF4J 4.x+ / GraphDB 10.x+ (default) |
| Parser support | yes (backward compatible) | yes |

---

## 9. References

- Format documentation (describes version 1):
  https://rdf4j.org/documentation/reference/rdf4j-binary/
- Reference implementation, `eclipse-rdf4j/rdf4j` repository,
  `core/rio/binary/src/main/java/org/eclipse/rdf4j/rio/binary/`:
  `BinaryRDFConstants.java`, `BinaryRDFWriter.java`, `BinaryRDFParser.java`,
  `BinaryRDFWriterSettings.java`
- Varint implementation:
  `core/common/io/src/main/java/org/eclipse/rdf4j/common/io/IOUtil.java`
