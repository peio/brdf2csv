# Motivation

## Knowledge graphs as the curated source of truth

Data that has been carefully curated, interlinked, and semantically enriched
deserves a home that preserves those qualities. A knowledge graph repository
such as Ontotext GraphDB is that home: it stores the data as RDF, keeps
identity and meaning explicit through IRIs, ontologies, and vocabularies,
validates it against schemas and shapes, enriches it through inference, and
makes it queryable with SPARQL. Every relationship that was worth modelling —
between instruments and issuers, assets and incidents, concepts and their
broader terms — remains a first-class, traversable fact rather than an
implicit join buried in application code.

The knowledge graph, in other words, is where the *whole* data lives: the
statements, their provenance, their semantics, and their connections.

## From knowledge graph to data product

The consumers of that data, however, increasingly live elsewhere. Modern data
platforms — warehouses, lakehouses, data marketplaces, internal data meshes —
speak tabular formats: CSV, Parquet, and the Apache open table formats built
on top of them (Iceberg, Delta Lake, Hudi). Publishing a *data product* on
such a platform means delivering well-described, versioned, tabular datasets
that analysts, data scientists, and downstream pipelines can consume with the
tools they already have.

This creates a natural, recurring pipeline: curate and maintain the graph in
GraphDB as the source of truth, then periodically export it and publish
tabular derivatives as data products. The graph does what graphs do best —
integration, semantics, consistency — while the tabular copies do what tables
do best: cheap columnar scans, SQL access, and frictionless distribution.

## Why RDF4J Binary RDF as the intermediary

The export step needs an interchange format, and the usual RDF serializations
are a poor fit for large repositories. Turtle and RDF/XML are expensive to
parse; N-Triples is simple but verbose, repeating every IRI in full on every
line.

GraphDB is built on the Eclipse RDF4J framework, and RDF4J defines a Binary
RDF format (`application/x-binary-rdf`) designed precisely for efficient,
lossless RDF exchange. It is compact — frequently repeated values such as
predicates and common IRIs are declared once and referenced by ID thereafter —
and it is fully streaming, cheap to parse, and preserves everything that
matters: named graphs, language tags, datatypes, blank nodes, namespace
declarations, and RDF-star statements. GraphDB can serve it directly from its
REST API with a single `Accept` header, making it the natural wire format for
periodic bulk exports.

## Why this package

RDF4J itself, being the reference Java implementation, can read and write
Binary RDF, and via its APIs the data can be re-serialized into tabular
outputs such as CSV. But that route runs through the JVM, and my working
environment — like that of most data engineering around modern platforms — is
Python.

There was, to my knowledge, no Python implementation of the RDF4J Binary RDF
format. Hence this package: a small, dependency-free Python utility that
parses Binary RDF (format versions 1 and 2, as written by current and older
GraphDB releases) and converts it to CSV, either in a lossless N-Triples-style
layout or in a wide, analysis-friendly layout with explicit type, language,
and datatype columns. From CSV, the remaining hop into Parquet or an open
table format is a one-liner in any modern data stack.

The result is a pipeline in which each component does the job it was built
for:

GraphDB (curation, semantics, SPARQL) → Binary RDF export (compact, lossless,
streaming) → brdf2csv (Python, no JVM) → CSV → Parquet / Iceberg / Delta →
published data product.

The graph remains the source of truth; the data products are its efficiently
refreshed, consumer-friendly projections.
