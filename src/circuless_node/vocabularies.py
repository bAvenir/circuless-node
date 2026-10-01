"""The controlled lists (N5, NFR9).

Three small vocabularies, committed and reviewed in a PR rather than configurable. That
is the point of calling them controlled: a record legal on one node has to be legal on
every other, or the catalogue's guarantee weakens to "somebody's node allowed this".

Adding an entry is a one-line change here plus a sentence in the PR saying who asked.
"""

from __future__ import annotations

from enum import StrEnum

#: Licences a resource may be published under (NFR9, `dct:license`).
#:
#: SPDX identifiers for software and data licences, EU Vocabularies URIs for the ones the
#: Commission's own catalogues use. Both are resolvable and both are what a DCAT-AP
#: consumer expects to see; an internal URL or an invented name is worse than no licence,
#: because it looks like one.
LICENCES: dict[str, str] = {
    # SPDX — https://spdx.org/licenses/
    "CC-BY-4.0": "https://spdx.org/licenses/CC-BY-4.0.html",
    "CC-BY-SA-4.0": "https://spdx.org/licenses/CC-BY-SA-4.0.html",
    "CC-BY-NC-4.0": "https://spdx.org/licenses/CC-BY-NC-4.0.html",
    "CC0-1.0": "https://spdx.org/licenses/CC0-1.0.html",
    "ODbL-1.0": "https://spdx.org/licenses/ODbL-1.0.html",
    "Apache-2.0": "https://spdx.org/licenses/Apache-2.0.html",
    "MIT": "https://spdx.org/licenses/MIT.html",
    # EU Vocabularies — http://publications.europa.eu/resource/authority/licence
    "EUPL-1.2": "http://publications.europa.eu/resource/authority/licence/EUPL_1_2",
    "COM_REUSE": "http://publications.europa.eu/resource/authority/licence/COM_REUSE",
    # The one a CIRCULess partner is most likely to need: shared under an agreement and
    # not otherwise reusable. Named explicitly so that "restricted" is a deliberate choice
    # rather than the absence of one.
    "CIRCULESS-AGREEMENT-ONLY": ("https://circuless.bavenir.eu/licences/agreement-only"),
}


class Theme(StrEnum):
    """`dcat:theme`, from the value-chain phase vocabulary.

    Four phases, fixed by the project rather than by us. A free-text theme would make the
    catalogue's faceted search meaningless within a month.
    """

    MATERIAL_CHARACTERISATION = "material-characterisation"
    PROCESSING = "processing"
    PRODUCT_DEVELOPMENT = "product-development"
    VALUE_CHAIN = "value-chain"


class Classification(StrEnum):
    """How sensitive the content is (D22).

    A **BVR-operated node refuses `sensitive`** — see `settings.NodeOperator`. The point is
    not that BVR's node is less secure; it is that BVR should not be holding a partner's
    sensitive data on the partner's behalf, whatever the node's security posture.
    """

    SYNTHETIC = "synthetic"
    NON_SENSITIVE = "non-sensitive"
    SENSITIVE = "sensitive"


class Discoverability(StrEnum):
    """Who may see that this resource **exists** (NFR4).

    Distinct from `Visibility`, and the two are easy to confuse. This one governs
    metadata: what reaches the Cloud catalogue and what a search returns. It never grants
    access to anything.

    Defaults to `hidden`: a resource just registered is not advertised anywhere until
    someone says otherwise.
    """

    HIDDEN = "hidden"
    CATALOGUE = "catalogue"

    #: **Not available in the beta** (design §5.2) — `resources.py` refuses it.
    #:
    #: It is reserved rather than wrong: `public` is meant to mean discoverable
    #: *anonymously*, and D21 allows no anonymous access at all, so there is nothing for
    #: it to mean yet. The member stays so that the day anonymous discovery arrives it
    #: is added rather than redefined — and so that a record carrying it, from a node
    #: older than this rule, is recognised rather than unparseable.
    PUBLIC = "public"


class Visibility(StrEnum):
    """Who may **read or invoke** the resource itself (NFR4).

    Decided by `decide()` (N6), never here. Defaults to `org`: the owning organisation
    and nobody else.

    `public` means any authenticated user or service — never anonymous, and never a node
    (D14, D21).
    """

    PRIVATE = "private"
    ORG = "org"
    AGREEMENT = "agreement"
    PUBLIC = "public"


class ResourceKind(StrEnum):
    DATASET = "dataset"
    SERVICE = "service"


class Shape(StrEnum):
    """How the content is held. `service` has no stored content at all."""

    FILE = "file"
    BUCKET = "bucket"
    SERVICE = "service"


class ResourceStatus(StrEnum):
    """Two-stage deletion (D25, N20 in M3).

    `DELETE` will mark a resource `withdrawn`: `decide()` denies it and the catalogue
    record is withdrawn, then a purge job removes the data after `purge_after`. The state
    exists from the start so that every query written before N20 already accounts for it
    — retrofitting "and not withdrawn" across a codebase is how one query gets missed.
    """

    ACTIVE = "active"
    WITHDRAWN = "withdrawn"
