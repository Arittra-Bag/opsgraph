"""Hosting-specific guidance for the shared PostgreSQL connector."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Literal

PostgresHosting = Literal[
    "local",
    "self_hosted",
    "supabase",
    "neon",
    "aws_rds",
    "google_cloud_sql",
    "azure",
    "digitalocean",
]


@dataclass(frozen=True, slots=True)
class HostingGuide:
    id: PostgresHosting
    name: str
    summary: str
    endpoint: str
    network: str
    tls: str
    steps: tuple[str, ...]
    checks: tuple[str, ...]
    documentation: str

    def as_dict(self) -> dict:
        return {
            **asdict(self),
            "validation": "Guidance available. Your endpoint must pass inspection and readiness.",
        }


HOSTING_GUIDES = (
    HostingGuide(
        "local",
        "Local PostgreSQL",
        "A database on this machine or a local development service.",
        "Use an explicit loopback host and port, or an operator-managed Unix socket.",
        "The database must be reachable from the backend. Container localhost is container-local.",
        "Loopback connections may run without TLS. All non-loopback "
        "destinations require verify-full.",
        (
            "Use a dedicated read-only login, never the database owner.",
            "Configure the private DSN through the launcher, then choose exact tables below.",
        ),
        (
            "Verify the PostgreSQL service and port.",
            "Check database CONNECT, schema USAGE, and table SELECT.",
        ),
        "https://www.postgresql.org/docs/current/libpq-connect.html",
    ),
    HostingGuide(
        "self_hosted",
        "Remote or self-hosted",
        "PostgreSQL on your server, VM, container, or private network.",
        "Use the server's PostgreSQL hostname and actual listening port.",
        "Allow the backend's network route through firewalls or an existing "
        "VPN. OpsGraph does not create tunnels.",
        "Set sslmode=verify-full and configure a trusted CA. The "
        "certificate must match the hostname.",
        (
            "Have the administrator provision a dedicated SELECT-only login for the exact tables.",
            "Place the CA on the backend host, or mount it into the container.",
            "Enter the DSN privately in launcher configuration or the "
            "approved backend environment.",
        ),
        (
            "Verify DNS and network reachability from the backend.",
            "Check certificate hostname and CA path.",
        ),
        "https://www.postgresql.org/docs/current/libpq-ssl.html",
    ),
    HostingGuide(
        "supabase",
        "Supabase",
        "Managed PostgreSQL with direct and pooled connection choices.",
        "Use a direct endpoint when IPv6 is reachable, or the session "
        "pooler for an IPv4-only backend.",
        "Check project network restrictions and the chosen endpoint's address family.",
        "Use sslmode=verify-full and the project CA where required. "
        "Configure trust on the backend.",
        (
            "Open the project's Connect panel and choose direct or session "
            "mode for this persistent application.",
            "Provision a dedicated read-only role. Use the pooler's "
            "required role and project username format.",
            "Configure the private connection, then inspect exact approved tables.",
        ),
        (
            "An IPv6-only direct endpoint may be unreachable on an IPv4-only network.",
            "Transaction-pooler behavior needs separate qualification. "
            "Start with direct or session mode.",
            "A Supabase Data API URL is not a PostgreSQL endpoint.",
        ),
        "https://supabase.com/docs/guides/database/connecting-to-postgres",
    ),
    HostingGuide(
        "neon",
        "Neon",
        "Serverless PostgreSQL with direct and pooled endpoints.",
        "Start with the branch's direct PostgreSQL endpoint from its connection panel.",
        "Check endpoint availability and any project IP or private-network restrictions.",
        "Use sslmode=verify-full with a trusted CA and the exact Neon hostname.",
        (
            "Choose the database and branch, then provision a dedicated SELECT-only role.",
            "Keep the direct endpoint for initial qualification. Configure "
            "its private DSN on the backend.",
            "Inspect the source and approve its bounded readiness check.",
        ),
        (
            "A suspended compute may need time to become available before an explicit retry.",
            "Pooled endpoints need their own reconnect and transaction qualification.",
        ),
        "https://neon.com/docs/connect/connect-securely",
    ),
    HostingGuide(
        "aws_rds",
        "AWS RDS PostgreSQL",
        "Managed PostgreSQL in an AWS network.",
        "Use the RDS instance endpoint, database name, and configured port.",
        "Permit the backend through security groups and a reachable VPC, "
        "VPN, or approved public route.",
        "Use sslmode=verify-full and the current RDS CA bundle through sslrootcert.",
        (
            "Have an administrator create a dedicated SELECT-only role, "
            "separate from the master user.",
            "Make the endpoint reachable from the backend and install the RDS CA bundle there.",
            "Configure the private DSN, then inspect and verify the approved scope.",
        ),
        (
            "A private RDS endpoint is not reachable without a private network route.",
            "IAM token renewal is operator-managed. OpsGraph does not "
            "generate or refresh IAM tokens.",
            "Aurora PostgreSQL endpoints need separate qualification.",
        ),
        "https://docs.aws.amazon.com/AmazonRDS/latest/UserGuide/PostgreSQL.Concepts.General.SSL.html",
    ),
    HostingGuide(
        "google_cloud_sql",
        "Google Cloud SQL",
        "Cloud SQL for PostgreSQL with a direct route or local Auth Proxy.",
        "Prefer an independently configured Cloud SQL Auth Proxy bound to "
        "loopback on the backend host.",
        "The proxy needs authorized cloud credentials and a route to the "
        "instance. Private IP still needs VPC reachability.",
        "A loopback proxy secures its upstream connection. Direct remote "
        "connections require verify-full and appropriate CA/client "
        "certificates.",
        (
            "Configure and start the Auth Proxy separately using Google's instructions.",
            "Use a dedicated SELECT-only database login and the proxy's loopback host and port.",
            "In containers, run the proxy in the same network namespace or "
            "configure a verified remote TLS route.",
        ),
        (
            "Cloud IAM permission to use the proxy does not replace database SELECT grants.",
            "OpsGraph does not provision proxies or refresh cloud authentication tokens.",
            "A proxy bound to another host is a remote destination, requiring verified TLS.",
        ),
        "https://docs.cloud.google.com/sql/docs/postgres/connect-auth-proxy",
    ),
    HostingGuide(
        "azure",
        "Azure PostgreSQL",
        "Azure Database for PostgreSQL Flexible Server.",
        "Use the server's PostgreSQL hostname and connection details.",
        "Configure firewall access or a private route with working private DNS for the backend.",
        "Use sslmode=verify-full and Azure's current trusted root certificates.",
        (
            "Provision a dedicated read-only database login rather than "
            "using the server administrator.",
            "Check firewall rules or private networking, then configure the CA on the backend.",
            "Store the DSN privately and inspect the exact permitted tables.",
        ),
        (
            "Use the server hostname for certificate verification, not an IP replacement.",
            "Microsoft Entra token acquisition and renewal are operator-managed.",
        ),
        "https://learn.microsoft.com/en-us/azure/postgresql/security/security-tls-how-to-connect",
    ),
    HostingGuide(
        "digitalocean",
        "DigitalOcean PostgreSQL",
        "DigitalOcean Managed PostgreSQL with trusted-source controls.",
        "Use the host and port from Connection Details. Do not assume port 5432.",
        "Add the backend as a trusted source or use a reachable private-network route.",
        "Use the provider's verify-full connection settings and its "
        "required CA or supported system trust.",
        (
            "Create a dedicated SELECT-only login, separate from the administrative account.",
            "Review trusted sources, endpoint type, port and certificate settings.",
            "Enter the private DSN on the backend, then inspect and verify the approved scope.",
        ),
        (
            "Connection Details may show sslmode=require. Full hostname "
            "verification is required here.",
            "Private endpoint access depends on the backend's network location.",
        ),
        "https://docs.digitalocean.com/products/databases/postgresql/how-to/connect/",
    ),
)


def hosting_guide(profile: str) -> HostingGuide:
    return next(item for item in HOSTING_GUIDES if item.id == profile)
