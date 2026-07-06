# Working with large Binary RDF files

This document collects the design decisions, measured optimizations, and
practical advice for converting large RDF4J Binary RDF exports — from
hundreds of millions to billions of statements — with brdf2csv. All figures
below were measured on 1M-statement fixtures (CPython 3.12, single core);
absolute numbers will vary by machine, but the ratios are what matter.

## Memory: everything streams

The converter is fully streaming and runs in constant memory regardless of
input size. Input is consumed through a 1 MiB chunk buffer, statements are
parsed one record at a time, and CSV rows are flushed in batches of 8,192.
Only two structures grow at all, and both are bounded: the value-reference
table, which the RDF4J writer itself keeps small by recycling IDs, and the
serialization memo, which is capped at 2^20 entries and cleared if a
pathological file would exceed it. A 500-million-statement export needs no
more RAM than a 500-statement one. There is consequently no need to split
input files for memory reasons — only, possibly, for parallelism (see
below).

## Throughput: what it is and where it goes

Sustained conversion rates on CPython are roughly 260–280k statements per
second for exports that make heavy use of value references (the normal case
for real RDF4J/GraphDB output) and around 150k/s for the worst case of fully
inline values with unique literals. As a rule of thumb, 100M statements
convert in 6–10 minutes and 500M in about half an hour to an hour.

Profiling showed the cost is concentrated in three places: reading the
byte-level structure, decoding strings, and writing CSV. The first was
attacked directly; the latter two are already C code inside CPython
(`bytes.decode` and the `csv` module) and set the practical ceiling for the
pure-Python implementation.

## Optimizations built into the converter

The parser avoids per-byte stream reads entirely: input is refilled in 1 MiB
chunks and all record, value-type, and varint bytes are read by indexing the
in-memory buffer. This eliminated roughly nine million function calls per
million statements and, notably, made gzipped input essentially free (see
below). The record/value hot path is inlined into a single loop with
local-variable state rather than method dispatch, and the 4-byte integer
reads of format version 1 use a precompiled `struct.Struct`.

On the output side, serialized N-Triples strings are memoized per term.
RDF4J writers emit repeated values as references to shared objects, and
predicates and class IRIs repeat constantly in real data, so most cells hit
the cache. Literal objects are deliberately *not* memoized: their labels are
the mostly-unique, memory-heavy part of a dataset, and caching them costs
more than it saves. Literal escaping uses a compiled regex as a fast path —
one C-level scan, with an immediate return for the overwhelming majority of
literals that contain no characters needing escapes. Rows are written with
`writerows` in batches rather than one call per row.

Two attractive-looking optimizations were tried, measured, and reverted,
and are documented here so they are not re-attempted: an identity-based
(`id()`) memo made the inline-heavy case twice as *slow*, because it pinned
every freshly parsed term in memory and paid millions of dict insertions for
zero hits; and `str.translate` for escaping lost to the regex fast path
because the common case is a literal that needs no escaping at all. The
general lesson stands: at this level, intuition fails and only benchmarks
decide.

## Compression: use it freely, on both ends

Gzipped input costs almost nothing: after the chunked-buffer rework,
`.brf.gz` input converts at the same speed as uncompressed input (measured
259k/s vs. 261k/s), because decompression happens in large C-level reads.
Since Binary RDF still contains long repeated IRI strings, gzip typically
shrinks exports several-fold, so compressing the export is nearly always
worth it for storage and transfer. Compression is auto-detected both for
files ending in `.gz` and for gzipped data arriving on stdin.

Gzipped *output* is equally cheap to request — pass `-o export.csv.gz` — and
usually more valuable, since the CSV is larger than the BRDF it came from.
If the CSV is destined for a warehouse staging area (Snowflake, BigQuery,
Athena and the rest all ingest gzipped CSV natively), writing it compressed
saves the largest file in the pipeline.

## Previewing and monitoring long runs

Before committing to a multi-hour conversion, sanity-check the file with
`--limit 100`, which parses only the first hundred statements and exits.
This verifies the format version, charset, and column layout in a second.
For the full run, `--progress` prints a running statement count to stderr
every 100,000 statements (the interval is configurable), which makes stalls
and throughput regressions visible without instrumenting anything.

When pulling directly from GraphDB, prefer streaming into the converter on
Linux (`curl -s -H 'Accept: application/x-binary-rdf' ... | brdf2csv -`),
which overlaps network transfer with parsing and never lands the
intermediate file on disk. On Windows PowerShell, download to a file first —
PowerShell 5.1 re-encodes pipeline data as text and corrupts binary streams.

## Interpreter choice: PyPy

Running the unmodified converter under PyPy yields a further 1.2–1.5×
(measured up to ~330k statements/s). The gain is smaller than PyPy's usual
factor because the workload is already dominated by C-level decode and CSV
routines, but it is entirely free: `pypy3 brdf2csv.py ...` with no code
changes. For recurring large batch jobs this is the first lever to pull.

## Compiled fast path: Cython

The package includes a typed Cython prototype of the parse loop
(`_brdfc.pyx`, with pre-generated C so target machines need only a
compiler). Measured results: about 1.9× overall (513k/s on ref-heavy data,
277k/s inline-heavy), and 5–8× on parsing and serialization in isolation
(0.8–1.2M statements/s). The gap between those two numbers is Python's
`csv` module, which after the Cython port accounts for 60–65% of remaining
runtime — meaning further gains require moving CSV output itself into
compiled code, not just parsing. The prototype also demonstrates a
structural optimization only available at that layer: values are serialized
to their final N-Triples strings once, at declaration time, so every
subsequent reference is a plain dictionary lookup of a finished string.

Beyond Cython, a standalone Rust binary (owning both parsing and CSV output
via the `csv` crate) is the realistic path to roughly 1M+ statements/s.
Extrapolating from the isolated parse rate, that turns a 500M-statement
conversion from ~30 minutes into ~5. Whether that is worth 2–4 days of
implementation plus a toolchain is a scheduling question, not a technical
one: at current speeds, even very large repositories convert comfortably
within a nightly batch window.

## Why the conversion does not parallelize (and what to do instead)

The Binary RDF format is inherently sequential. The value-reference table is
stateful — a statement may reference a value declared megabytes earlier, and
IDs are recycled — so the stream cannot be split at arbitrary offsets and
handed to workers. Shipping parsed terms from a reader process to serializer
workers was also evaluated and rejected: pickling tuples of strings across
process boundaries costs more than the serialization it would offload.

Parallelism therefore belongs *upstream*. If wall-clock time matters more
than simplicity, split the export at the source: request one Binary RDF file
per named graph (GraphDB's statements endpoint accepts a `context`
parameter), or partition by graph pattern with SPARQL CONSTRUCT queries, and
run one converter process per file. The converters are independent and scale
linearly with cores; the resulting CSVs can be loaded as separate partitions,
which most lakehouse formats prefer anyway.

## Downstream: getting to Parquet and open table formats

CSV is the interchange hop, not the destination. For large data products,
convert the CSV to Parquet promptly — it is smaller, columnar, and preserves
types. The wide output format (`--format wide`) is designed for this: raw
lexical values with explicit type, language, and datatype columns map
cleanly onto a typed Parquet schema, whereas the lossless N-Triples format
is better when round-tripping back to RDF matters. With PyArrow the hop is a
few lines, and it streams too:

```python
import pyarrow.csv as pc, pyarrow.parquet as pq
writer = None
for batch in pc.open_csv("export.csv.gz"):
    if writer is None:
        writer = pq.ParquetWriter("export.parquet", batch.schema)
    writer.write_batch(batch)
writer.close()
```

From Parquet, registration in Iceberg, Delta Lake, or Hudi follows each
platform's normal ingestion path. Partitioning by the `graph` column is the
natural first choice for RDF-derived tables, particularly when the upstream
export was already split per named graph.

## Summary of levers, in the order to pull them

For most workloads the defaults are already right: stream from GraphDB,
keep everything gzipped, preview with `--limit`, monitor with `--progress`.
If a recurring job is too slow, switch the interpreter to PyPy (free,
~1.3–1.5×). If that is still insufficient, build the bundled Cython
extension (~1.9×, one compile). Beyond that, split exports per named graph
and run converters in parallel (linear scaling), and only then consider a
Rust implementation (~5–10×, days of work). At every stage, measure on your
own data before and after — the two reverted optimizations above are the
proof that plausible ideas can make things slower.
