# Third-party notices

OpsGraph's original source is licensed under
[Apache-2.0](../../LICENSE). Dependencies and bundled native components retain
their own licenses. Every native bundle preserves the original dependency wheels
and includes the supplemental notices below.

| Component | Included material |
| --- | --- |
| pglast 7.18 | [GPLv3](notices/GPL-3.0.txt), [project header](notices/pglast-7.18-project-header.txt), [libpg_query license](notices/libpg_query-LICENSE.txt), [PostgreSQL notice](notices/PostgreSQL-17.7-COPYRIGHT.txt), [Bison exception](notices/libpg_query-Bison-NOTICE.txt), [protobuf-c notice](notices/libpg_query-protobuf-c-NOTICE.txt), and [xxHash notice](notices/libpg_query-xxhash-NOTICE.txt) |
| psycopg binary dependencies | [LGPLv3](notices/LGPL-3.0.txt), PostgreSQL [18.4](notices/PostgreSQL-18.4-COPYRIGHT.txt) and [18.6](notices/PostgreSQL-18.6-COPYRIGHT.txt), OpenSSL [3.5.8](notices/OpenSSL-3.5.8-LICENSE.txt) and [3.6.3](notices/OpenSSL-3.6.3-LICENSE.txt), [Kerberos](notices/Kerberos-1.22.2-NOTICE.txt), and [OpenLDAP](notices/OpenLDAP-2.6.14-LICENSE.txt) |
| LangSmith 0.11.0 | [MIT license](notices/langsmith-0.11.0-LICENSE.txt) |

Each native bundle generates its own `dependency-inventory.json` from the exact
application wheel and target-selected wheelhouse. The inventory records wheel
hashes and sizes, declared package/license metadata, and the paths and hashes of
observed license, notice, embedded SBOM, and native-library members. Its hash is
bound into that bundle's `build-identity.json` and `manifest.json`. Platform
inventories can differ and must stay with the bundle they describe.

The stable [source manifest](notices/source-manifest-1.0.0.json) identifies the
verified upstream source archives and supplemental notices used to build
`opsgraph-1.0.0-third-party-sources.tar.gz`. The source supplement includes that
manifest, the recorded source files, the listed notices, and checksums. Original
wheel license/notice members remain in the wheels shipped by each native bundle.

These inventories and source records identify the delivered artifact set; they
are not legal or security certification. See [stable distribution](distribution.md)
for the complete 1.0.0 artifact contract.
