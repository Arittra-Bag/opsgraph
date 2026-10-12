# Connect PostgreSQL wherever you run it

OpsGraph uses one PostgreSQL connector for local, self-hosted, and managed
services. Hosting choices provide route-specific guidance. They do not create
cloud resources, open firewalls, provision credentials, or certify a provider.
Every endpoint must pass source inspection and its bounded readiness check.

## Start with a private connection

In **Sources → Add source**, follow the five steps: database host, private
connection, exact tables, inspection and review, then query check. The hidden
connection field accepts a PostgreSQL URI, not a provider HTTPS project address.
For remote URIs the browser requests `sslmode=verify-full`. Upload a PEM CA
certificate (up to 64 KB), use a CA path on the backend, or rely on supported
system trust. Private keys are rejected. Saving a connection does not contact
the database or model; discovery reads names and inspection reads metadata.
Only the separate, explicitly approved query check performs a bounded read.

The connection is stored in private backend files, not browser storage, reports
or SQLite evidence. A full reload retains saved connections, selected sources,
and recorded checks without displaying passwords. Browser storage contains only
navigation and saved source identifiers in addition to the workspace session key.
Unsaved passwords and drafts are discarded. An unfinished saved connection can
resume table selection without another paste.

The terminal alternative remains `opsgraph launch --configure`. It uses the same
connection validator. Operator-managed DSN environment references remain supported
and cannot be overwritten from the browser. The hosting choice changes guidance
and the locally bundled logo; it does not select a different SQL connector.

The browser reconnects an authenticated metadata notification stream using
`X-OpsGraph-Key`. Notifications refresh saved states only: they never run database
or model probes. The last pass or failure remains visible with its time, and the
last 30 results are available under **Earlier checks for this source**. Older
receipts remain in **Audit**. A historical pass does not bypass changed schema,
credential, source, deployment policy or model-readiness checks. Failures remain
visible after reload until explicitly checked again.

**Workspace connected** hides the editable key. **Disconnect and enter a key
again** clears this browser's access; saved backend records and keys remain intact.
No cookies or polling of provider APIs are needed for this flow.

Use a dedicated login with database CONNECT, schema USAGE, and SELECT on exact
approved tables. Keep it separate from owners and administrators. The Sources
page can generate a scoped role script for an administrator to review. OpsGraph
never executes that script. Effective write privileges cause inspection or
execution to fail closed, even inside a read-only transaction.

Remote connections require `sslmode=verify-full` by default. Use the exact
certificate hostname and an appropriate trusted CA through `sslrootcert` or
supported system trust. Place trust files on the backend filesystem, including
inside its container when applicable. Never weaken certificate verification to
fix a failed connection. Loopback connections and separately configured local
proxies have a different transport boundary. The proxy operator owns upstream
TLS, cloud authentication, and network routing.

## Hosting routes

| Hosting choice | Initial connection route | Check before inspection |
| --- | --- | --- |
| Local PostgreSQL | Explicit loopback host and port, or an operator-managed Unix socket | The database must be reachable from the backend process. Container localhost belongs to the container. |
| Remote or self-hosted | PostgreSQL hostname and actual listening port | DNS, firewall or VPN route, certificate hostname, trusted CA, and exact grants. |
| Supabase | Direct endpoint with IPv6 reachability, or session pooler for an IPv4-only backend | Project network restrictions and pooler username format. A Data API URL is not a PostgreSQL endpoint. Transaction pooling needs separate qualification. |
| Neon | Direct database and branch endpoint for initial qualification | Compute availability, exact branch and database, IP restrictions, and verified TLS. Pooled endpoints need their own reconnect and transaction qualification. |
| AWS RDS PostgreSQL | RDS instance endpoint through an approved VPC, VPN, or public route | Security groups, current RDS CA bundle, and a dedicated role. IAM token generation and renewal remain operator-managed. Aurora needs separate qualification. |
| Google Cloud SQL | Independently configured Auth Proxy bound to backend loopback, or a qualified direct route | Proxy credentials and upstream route. Private IP still needs VPC reachability. Direct remote connections need verified TLS and any required client certificates. |
| Azure Database for PostgreSQL | Flexible Server hostname through public access or a private network | Firewall, private DNS, current CA trust, and dedicated grants. Identity token generation and renewal remain operator-managed. |
| DigitalOcean Managed PostgreSQL | Host and actual port from Connection Details | Trusted sources or private route, current CA, and verified hostname. Connection examples using `require` need full verification configured for OpsGraph. |

Official instructions: [PostgreSQL](https://www.postgresql.org/docs/current/libpq-connect.html),
[Supabase](https://supabase.com/docs/guides/database/connecting-to-postgres),
[Neon](https://neon.com/docs/connect/connect-securely),
[RDS](https://docs.aws.amazon.com/AmazonRDS/latest/UserGuide/PostgreSQL.Concepts.General.SSL.html),
[Cloud SQL](https://cloud.google.com/sql/docs/postgres/connect-auth-proxy),
[Azure](https://learn.microsoft.com/en-us/azure/postgresql/flexible-server/how-to-connect-tls-ssl),
and [DigitalOcean](https://docs.digitalocean.com/products/databases/postgresql/how-to/connect/).
Provider consoles and certificate requirements can change. Use current official
instructions alongside the application guide.

## Verify the full path

1. Connect your workspace and open Sources. Choose a hosting option.
2. Confirm the private backend connection, exact schemas, and exact tables.
3. Save and inspect. This checks the connection, effective role, and physical
   schema without reading application values or calling a model.
4. Review the inspection. Approve one bounded readiness read on one table.
   It selects the constant `1`, reads at most one row, retains no source value,
   and uses a timeout of one second or less.
5. Continue to model setup if needed. Save and test the actual configured model.
6. Ask a bounded operational question. Inspect SQL, records, classifications,
   and limitations before accepting or sharing any conclusion.

Source changes invalidate readiness. Hosting guidance is metadata, not a
connector override. It cannot bypass transport validation, policy, table scope,
role checks, row limits, or external inference consent.

## Recover safely

Connection errors return a fixed category, a readable message, and repair steps.
The existing API `detail` remains a string. The optional `diagnostic` object has
`code`, `title`, `message`, and `steps`. No raw database exception, hostname,
connection URL, password, or local certificate path is returned.

Categories distinguish missing credentials, invalid configuration, TLS setup
and verification, DNS, network routes, connection timeout, authentication,
access grants, missing databases, capacity, query timeout, unsafe roles, and
unavailable scope. Classification can be generic when a driver provides too
little information. Repair the indicated configuration and retry explicitly.
OpsGraph does not downgrade TLS, retry credentials, open network access, or
switch databases automatically.

Existing databases, private networks, cloud accounts, and certificate files
remain operator-managed. Selecting a hosting option is not evidence that an
actual managed service has been tested.
