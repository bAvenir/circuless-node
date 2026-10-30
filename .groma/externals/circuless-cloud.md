---
type: C4 System
title: CIRCULess Cloud
status: stable
groma:
  id: circuless-cloud
  technology: HTTPS, REST
---

The control plane: catalogue, agreements, and the organisation and node registries. This node pushes a DCAT record for each tenant whenever its resources change, and pulls the provider-side agreements and the organisation map every 30 seconds. The Cloud never reaches into the node and never holds the data.
