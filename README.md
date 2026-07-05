# brdf2csv

Convert RDF4J Binary RDF (`application/x-binary-rdf`, as exported by Ontotext
GraphDB) to CSV. Pure Python, stdlib only, fully streaming (constant memory).

## Format support

| | Version 1 | Version 2 |
|---|---|---|
| Written by | older RDF4J / GraphDB 9.x | RDF4J 4.x / GraphDB 10.x (default) |
| String encoding | int32 length (UTF-16 code units) + UTF-16BE bytes | varint byte length + bytes in the charset declared in the header (UTF-8 in practice) |
| Value ref IDs | int32 | varint (unsigned LEB128) |

Both versions are auto-detected from the header. Implemented directly from the
authoritative RDF4J sources (`BinaryRDFParser.java`, `BinaryRDFWriter.java`,
`BinaryRDFConstants.java`, `IOUtil.java` in `eclipse-rdf4j/rdf4j`); note that
the documentation page at rdf4j.org describes version 1 only.

Handles: URIs, blank nodes, plain/lang/datatyped literals, value
declarations/references (including ID recycling), named graph contexts,
namespace declarations, comments, RDF-star quoted triples (serialized as
`<< s p o >>`), and gzipped input (auto-detected, including on stdin).

## Usage

```bash
# Basic conversion (lossless N-Triples-style terms, 4 columns)
python3 brdf2csv.py export.brf -o export.csv

# Analysis-friendly wide format (8 columns: raw lexical values +
# subject_type / object_type / object_lang / object_datatype / graph)
python3 brdf2csv.py export.brf --format wide -o export.csv

# Stream straight from GraphDB without touching disk
curl -s -H 'Accept: application/x-binary-rdf' \
  'http://localhost:7200/repositories/myrepo/statements' \
  | python3 brdf2csv.py - > export.csv

# Preview a huge export
python3 brdf2csv.py export.brf.gz --limit 100

# Extras
python3 brdf2csv.py export.brf --delimiter ';' --no-header \
  --progress 500000 --namespaces ns.csv -o out.csv
```

Exit code 1 with a message on stderr for malformed input (bad magic, unknown
version, truncation, dangling value references, invalid term positions).

## Output formats

**`ntriples` (default, lossless):** `subject,predicate,object,graph` with
terms in N-Triples syntax (`<uri>`, `_:bnode`, `"literal"@lang`,
`"lit"^^<dt>`). Literals with datatype `xsd:string` are emitted without the
datatype suffix per RDF 1.1. Rebuilding N-Quads from the rows is
`f"{s} {p} {o} {g} ."`.

**`wide` (analysis-friendly):** raw lexical values with explicit
`subject_type` / `object_type` (`uri`/`bnode`/`literal`/`triple`) and
`object_lang` / `object_datatype` columns; plain literals are normalized to
`xsd:string`. Convenient for pandas, Snowflake staging, or spreadsheet
inspection.

Embedded newlines, quotes and delimiters inside literals are handled by
standard CSV quoting.

## Testing

```bash
python3 test_brdf2csv.py
```

14 tests, including a reference *encoder* for both format versions (mirroring
`BinaryRDFWriter.java`), non-BMP characters (UTF-16 surrogate pairs), varint
boundaries, ID recycling, RDF-star, alternate v2 charsets, and malformed-input
handling. Additionally validated by rebuilding N-Quads from the CSV output and
checking graph isomorphism against the source data with rdflib.

Throughput: ~230k statements/sec single-threaded (≈7 min for a 100M-statement
export).

## Optional Cython fast path (prototype)

`_brdfc.pyx` is a typed Cython port of the parse loop (N-Triples output
path, v1 + v2/UTF-8), measured at ~1.9x overall vs pure Python (513k/277k
stmts/s on ref-heavy/inline-heavy data), verified byte-identical to the
pure-Python output. The pure-Python module works with no installation at
all -- the extension is strictly optional.

### Installing / building the extension

**Option A -- pip (recommended).** From the extracted archive directory:

```bash
pip install .
```

This installs `brdf2csv` as a command-line tool and builds the `_brdfc`
extension if a C compiler is present. Uses Cython if installed, otherwise
the bundled pre-generated `_brdfc.c`. If compilation fails (no compiler,
missing headers), the install still succeeds and the pure-Python parser
is used -- you lose speed, not functionality.

**Option B -- build in place, no installation.** Produces `_brdfc.*.so`
next to the sources:

```bash
python3 setup.py build_ext --inplace
```

**Option C -- bare gcc, no pip/setuptools/Cython.** For locked-down or
offline servers (e.g. RHEL/CentOS with system Python 3.6), only a C
compiler and the Python headers are needed:

```bash
# headers: 'yum install python3-devel' / 'apt install python3-dev'
cc -O2 -shared -fPIC $(python3-config --includes) _brdfc.c -o _brdfc.so
```

**Option D -- regenerate C from source** (requires Cython >= 3):

```bash
pip install cython
cythonize -i -3 _brdfc.pyx
```

### Build prerequisites by platform

- Debian/Ubuntu: `apt install build-essential python3-dev`
- RHEL/CentOS/Rocky: `yum install gcc python3-devel`
- The compiled `.so` is specific to the Python minor version and platform
  it was built on; rebuild after Python upgrades.

### Verifying and using it

```bash
python3 -c "import _brdfc; print('fast path OK')"
```

The prototype is not yet wired into the `brdf2csv` CLI. Programmatic use:

```python
from _brdfc import FastBRDF
import csv, sys

fp = FastBRDF(open("export.brf", "rb").read())  # whole file in memory
w = csv.writer(sys.stdout)
w.writerow(["subject", "predicate", "object", "graph"])
while True:
    rows = fp.next_rows(8192)   # list of (s, p, o, g) N-Triples strings
    if not rows:
        break
    w.writerows(rows)
# fp.namespaces / fp.comments are populated after parsing
```

Prototype limitations: N-Triples output format only (no wide format),
v2 inputs must be UTF-8 (raises NotImplementedError otherwise -- catch it
and fall back to `brdf2csv.BRDFParser`), and the input is read fully into
memory rather than streamed. The pure-Python module remains the reference
implementation.

## Files

- `brdf2csv.py` — the converter (stdlib only, Python 3.6+)
- `test_brdf2csv.py` — 14 unit tests incl. a reference encoder for v1/v2
- `_brdfc.pyx` / `_brdfc.c` — optional Cython fast-path prototype
- `setup.py` — builds/installs the extension (optional; degrades gracefully)
- `motivation.md` — why this package exists (graph-to-data-product pipeline)
- `README.md` — this file
