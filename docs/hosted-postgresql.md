# Connect PostgreSQL wherever you run it

OpsGraph uses one PostgreSQL connector for local, self-hosted, and managed
services. Hosting choices provide route-specific guidance. They do not create
cloud resources, open firewalls, provision credentials, or certify a provider.
Every endpoint must pass source inspection and its bounded readiness check.

## Start with a private connection

Run `opsgraph launch --configure` on the backend host. Choose the hosting option
and enter the connection through the hidden terminal prompt. The launcher keeps
it in a private configuration file. Advanced installations can use an approved
DSN environment-variable reference. Browser source fields accept the reference
name, never the connection URL. The hosting preference is stored as
`OPSGRAPH_POSTGRES_HOSTING` and can be changed for each source in Sources.

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
