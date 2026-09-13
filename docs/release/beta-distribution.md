# Beta distribution: source, licenses and replaceable dependencies

OpsGraph's original application source remains licensed under
[Apache-2.0](../../LICENSE). Dependencies retain their licenses. The integrated
bundle uses pglast, which is GPL-3.0-or-later; the combined distribution must be
conveyed under the applicable GPLv3 terms, while OpsGraph's original files keep
their Apache-2.0 grant. The bundle is not wholly Apache-licensed.

Apache-2.0 and GPLv3 are compatible in this direction. Preserving the original
Apache license and complying with the combined distribution's GPL obligations
are compatible actions; replacing the SQL validator is unnecessary for this
purpose. See the [Apache compatibility explanation](https://www.apache.org/licenses/GPL-compatibility)
and [GNU's parts/combined-work explanation](https://www.gnu.org/licenses/license-compatibility.html).
This record describes the packet and its limits, not legal certification.

## Files to publish together

Publish the exact OpsGraph wheel/bundle, its matching application source archive
and build instructions, and `opsgraph-0.1.0b1-third-party-sources.tar.gz`, each
with SHA-256 checksums. Put a clear source link next to the binary download.
The source supplement contains:

- Unmodified pglast7.18 source, including its vendored parser/build files;
  GPLv3, PostgreSQL/libpg_query and other extracted copyright notices.
- Psycopg3.3.5 and psycopg_c3.3.5 source distributions, plus the exact tagged
  Psycopg repository and its native-library build and binary-package scripts.
- orjson3.12.0 source for its declared MPL-covered components; its upstream
  source and wheels retain Apache/MIT/MPL notices.
- LGPLv3 and supplemental PostgreSQL18.6, OpenSSL3.5.8, Kerberos1.22.2 and
  OpenLDAP2.6.14 license/copyright material for the selected macOS wheel.
- A source/notice provenance record and per-file checksums.

The GPLv3 section6(d) network-distribution route is used: equivalent source
access accompanies object-code access, without a fee. Retain that source access
for the required duration. A build not being byte-for-byte independently
reproduced does not itself establish missing corresponding source. The exact
source files, build inputs and scripts still need to correspond to the conveyed
code. [GPLv3 text](notices/GPL-3.0.txt).

## Psycopg and user modification

Psycopg remains LGPL-3.0-only. Both GPLv3 and LGPLv3 texts accompany the packet.
No additional term restricts modifying these libraries or reverse engineering to
debug those modifications. Python imports load the installed libraries at run
time; the product does not authenticate or reject an interface-compatible
replacement at startup. The installer verifies the supplied unmodified package
hashes to prevent accidental or hostile substitution during installation.

To develop with a modified dependency, stop OpsGraph, retain/backup the workspace,
create a separate Python environment and install the application's matching
source plus the modified interface-compatible dependency there. Point the launch
command at a copy of the workspace. The source/build scripts are available so
users can rebuild the combination; this is not a promise that arbitrary modified
libraries remain compatible. Do not overwrite the only copy of investigation
history. See [LGPLv3](notices/LGPL-3.0.txt) and
[Psycopg installation options](https://www.psycopg.org/psycopg3/docs/basic/install.html).

## Exact scope and remaining boundaries

[beta-native-provenance.json](notices/beta-native-provenance.json) identifies the
selected macOS arm64 CPython3.11 pglast/Psycopg wheels, sources and new notices.
The tagged Psycopg3.3.5 build chooses libpq18.6, OpenSSL3.5.8, Kerberos1.22.2 and
OpenLDAP2.6.14. OpenSSL3.5.8 is additionally visible in the selected macOS binary.
This is upstream build/source evidence, not an independent rebuild attestation.
Original license files inside all dependency wheels must remain intact.

Other native wheels retain their supplier license/SBOM material. The existing
inventory records those actual members; it is not exhaustive transitive-source
certification. These supplemental sources/notices address the concrete recorded
macOS omissions without declaring every third-party component security-certified.
Windows and Linux binary contents require their own final inventory. In
particular, Linux's secondary libcrypto1.1.1k patch provenance remains unresolved;
that version string alone does not prove an unpatched vulnerability. Do not
transfer macOS native evidence to another platform or relabel historical results.
