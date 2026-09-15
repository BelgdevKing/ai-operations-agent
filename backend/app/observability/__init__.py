"""Observability: metrics, tracing, pricing, and the vocabulary they share.

Cross-cutting infrastructure, not a business layer. Nothing here reads or
writes the database, nothing here knows what a shipment is, and nothing here
imports a provider SDK. The rule that keeps it that way is simple: this package
is imported *by* services, and imports nothing from them.

Four things live here and no more:

``metrics``
    An in-process registry of counters, histograms and gauges, and the text
    exposition of them. Deliberately small and dependency-free.
``pricing``
    A versioned, model-keyed price book that turns token counts into money -
    or into an honest "unknown" when a model has no configured price.
``names``
    The metric names, the label vocabularies and the span-attribute allow-list,
    in one file, so a value that leaves this process can be checked against a
    closed set rather than trusted.
``tracing``
    Spans for one request: W3C trace context in, OpenTelemetry's data model
    out, an allow-list on every attribute, and an exporter that cannot fail a
    request. Off unless a deployment turns it on.

What is **not** here: usage aggregation, which is a tenant-scoped database
question and belongs in a repository.
"""

from __future__ import annotations
