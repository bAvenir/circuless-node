---
type: C4 Component
title: Data transfer and storage
status: stable
groma:
  id: data-transfer
  parent: node-application
  code:
    - scanner: python
      file: src/circuless_node/transfer.py
    - scanner: python
      file: src/circuless_node/upload.py
    - scanner: python
      file: src/circuless_node/storage.py
  group: Resources and transfer
  technology: fsspec
description: Serving a dataset's bytes, taking an upload, and keeping both inside the tenant's own directory.
---

A dataset is either one file or a set of objects. A bucket's manifest lists its objects, and each object is decided and logged on its own rather than once for the set.

There is no Range support. The node sends Accept-Ranges: none and always returns the whole file, which is a decision rather than a gap: resumable download is an optimisation, and every line in this module is security-relevant surface. If it is ever added, Range must be parsed after the decision and the log write, because a 416 carries Content-Range: bytes */size and would otherwise disclose a file's size to a caller with no agreement.

Paths are confined: the request path is normalised and then refused if it climbs out with .., is absolute, or looks like a URL. Storage resolves everything against the tenant's directory and refuses anything that escapes it.

Paths are confined: the request path is normalised and then refused if it climbs out with `..`, is absolute, or looks like a URL. Storage resolves everything against the tenant's directory and refuses anything that escapes it.

Deletion is two-stage. Withdrawing marks the resource so the decision function refuses it and the catalogue record is withdrawn at once; the bytes go later.

## Relationships

| Source | Target | Description | Technology |
| --- | --- | --- | --- |
| [src/circuless_node/transfer.py](../../../../../../src/circuless_node/transfer.py) | [src/circuless_node/decide.py](../../../../../../src/circuless_node/decide.py) | Authorizes the read | Python call |
| [src/circuless_node/transfer.py](../../../../../../src/circuless_node/transfer.py) | [src/circuless_node/access_log.py](../../../../../../src/circuless_node/access_log.py) | Records the decision | SQLModel |
