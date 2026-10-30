---
type: C4 Component
title: DCAT record
status: stable
groma:
  id: dcat
  parent: node-application
  code:
    - scanner: python
      file: src/circuless_node/dcat.py
  group: Resources and transfer
description: Renders a resource as the catalogue entry other organisations will see.
---

Builds the DCAT-AP document the Cloud catalogue publishes: a dataset as `dcat:Dataset` with a distribution, a service as `dcat:DataService` with its endpoint description.

What it leaves out matters as much. The distribution points at **the node's own address**, never the upstream one — a consumer who had the real endpoint could go round the node, past the decision, the agreement and the log. A service's upstream URL is shown only to someone who may manage the resource.
