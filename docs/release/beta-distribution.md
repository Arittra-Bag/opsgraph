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

- Matching source distributions for all locked Python runtime/provider packages;
  Psycopg's binary implementation is supplied as `psycopg_c` source together with
  the exact tagged Psycopg repository and binary/native build scripts.
- Original pglast/parser source and GPL notices, plus source for the declared
  MPL-covered components in orjson, certifi and tqdm.
- PostgreSQL18.6/18.4, OpenSSL3.5.8/3.6.3, Kerberos1.22.2, OpenLDAP2.6.14 and
  libxcrypt4.5.2 sources, with the selected platform build inputs.
- Source RPMs, including downstream patches, for the Linux wheel's
  SBOM-identified CentOS libraries: Kerberos, Cyrus SASL, keyutils, e2fsprogs,
  libselinux and PCRE. These are source archives, not packages to install.
- Original wheel notices, supplemental notices, a source manifest and per-file
  checksums. [beta-source-manifest.json](notices/beta-source-manifest.json)
  records the upstream URLs and hashes; archives are not modified.

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

## Exact platform inventory

[beta-wheel-inventory.json](notices/beta-wheel-inventory.json) records every
selected dependency wheel hash, native member hash, original notice and supplier
SBOM reference for the CPython3.11 bundles. The macOS and Ubuntu bundles each
contain 53 dependency wheels; Windows contains 54. The difference is platform
conditional dependencies, not an interchangeable wheelhouse.

[beta-native-provenance.json](notices/beta-native-provenance.json) records the
native review. macOS and Linux use the tagged Psycopg build's libpq18.6 and
OpenSSL3.5.8. Windows DLL resources identify libpq18.4 and OpenSSL3.6.3; its
build uses vcpkg, so the Unix build-version variables do not establish its versions.
Matching Windows notices are included separately.

The previously noted secondary OpenSSL1.1.1k is absent from every native member
in the selected Linux CPython3.11 wheelhouse. It is not a finding against this
release artifact. Linux's additional `libcrypt` is libxcrypt4.5.2: the pinned
manylinux image selects its source hash, the original library hash matches the
auditwheel filename suffix, and the bundled ELF code and read-only data sections
match the image library. The source and build scripts are supplied.

This evidence identifies the supplied files and their sources; it is not an
independent native rebuild or a security certification of every component.
Original wheel license/SBOM files remain intact. Other Python versions,
architectures and container images require their own inventory. Historical alpha
inventory records remain dated evidence and do not describe these release assets.
