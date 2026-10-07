# Public hostname and Gitea's built-in TLS

This mode uses Gitea's own HTTPS/ACME implementation, not a reverse-proxy service.
It remains off until tlsEnabled is explicitly true and deployment settings are
provided. DNS records, router configuration and actual certificate issuance are
operator actions; the implementation does not change them.

Official references:
- https://docs.gitea.com/administration/https-setup/
- https://docs.gitea.com/administration/config-cheat-sheet/#server-server
- https://letsencrypt.org/repository/ (read the current subscriber agreement)

## Configuration

settings.tls.example.yaml is a complete example for a new/disposable deployment.
For the existing raspberrypi deployment, retain the actual image digest, project,
data/config volume names, backup destination and backup policy. Add only the TLS
settings below; do not replace those existing values with example placeholders.

```yaml
tlsEnabled: true
publicHostname: git.example.net
acmeEmail: admin@example.net
acmeAcceptTos: false
httpsPort: 8443
httpRedirectPort: 8080
```

Replace the hostname and email. Read your CA's current terms, then explicitly set
acmeAcceptTos to true. Until all required fields are valid, the settings parser
refuses to enable TLS. Existing JSON configurations also support these fields.
The selected public hostname becomes the canonical HTTPS URL and the hostname
advertised for SSH. SSH retains sshPort and sshListenPort (normally both 2222).

The main Compose file continues to support local HTTP without the TLS override.
When enabled, compose-env.sh exports COMPOSE_FILE with both compose.yaml and
compose.tls.yaml. Management commands select the same files and environment.
Use plain podman-compose commands after sourcing; specifying only
`-f compose.yaml` would bypass the TLS override.
Remove old hardcoded ROOT_URL, DOMAIN, protocol or ACME entries from the server
block; use the supplied variable-driven base file and TLS override. Avoid duplicate
YAML keys. Do not hardcode a host port in the canonical ROOT_URL.

## Rootless ports, DNS and router

| Public connection | Router destination | Container destination |
|---|---|---|
| TCP 80 | raspberrypi:8080 | Gitea redirect/HTTP-01 listener:8080 |
| TCP 443 | raspberrypi:8443 | Gitea HTTPS listener:3000 |
| TCP 2222 | raspberrypi:2222 | Gitea SSH listener:sshListenPort |

These are the defaults; use your configured httpsPort/httpRedirectPort/sshPort
for the router's destination ports. No host port below 1024 is bound, and no
privileged-port sysctl changes are required. Neither Gitea's plaintext web port
nor the Podman API socket should be forwarded publicly. The HTTP listener serves
ACME validation and redirects ordinary requests to the canonical HTTPS URL.

Create the hostname's A record and keep it current with your existing DDNS.
Publish an AAAA record only if IPv6 reaches the same instance and its required
ports; a stale/unreachable AAAA record can break validation. Point the forwarding
rules at the Pi's stable LAN address. Test NAT loopback so the same public URL
works from home. Use https://YOUR_HOSTNAME/ both inside and outside the LAN.
Access by https://raspberrypi:8443/ will not match a certificate issued to the
public hostname.

## Verify on a disposable deployment first

Build the updated management image and run tests:

```sh
podman build -t localhost/gitea-podman-manager:5 -f Containerfile .
export GITEA_MANAGER_IMAGE=localhost/gitea-podman-manager:5
./scripts/test.sh
```

Use a separate local project directory, project/volume names, hostname and free
ports for live testing. Do not change the running production instance for a
staging test. With the test hostname's DNS and router rules in place, configure:

```yaml
acmeUrl: https://acme-staging-v02.api.letsencrypt.org/directory
```

Staging certificates are intentionally NOT trusted by browsers or normal HTTPS
clients. Inspect Gitea logs to verify issuance. For an inspection-only command
use the already installed openssl, or its containerized equivalent if missing:

```sh
openssl s_client -connect YOUR_HOSTNAME:443 -servername YOUR_HOSTNAME -showcerts </dev/null
```

HTTPS readiness deliberately rejects staging certificates. Consequently a
restore/upgrade may report readiness failure even while a staging server is
running. Inspect its logs and do not repeatedly restore or interrupt migrations
merely because the staging CA is untrusted. Do not disable verification in the
production readiness checker or in Git clients.

Production and staging use separate persistent caches: /var/lib/gitea/https and
/var/lib/gitea/https-staging. A custom HTTPS ACME directory URL is supported via
acmeUrl and receives a separate deterministic cache directory. This prevents a
staging certificate from being reused when selecting the production CA.

## Production rollout

1. While TLS remains disabled, take a verified backup and record its snapshot ID.
2. Confirm DNS, DDNS, stable LAN address, router mappings, NAT loopback and free
   destination ports. Choose the final hostname and contact email.
3. Configure production TLS and terms acceptance. Remove the staging acmeUrl
   setting (or set it to https://acme-v02.api.letsencrypt.org/directory).
4. Review the effective Compose configuration and apply it:

```sh
source scripts/compose-env.sh &&
podman-compose config
# After reviewing the hostname, ports and existing volume mappings:
podman-compose up -d server backup
```

The server restarts to switch protocol. The backup sidecar remains on its network;
registration policy, repository visibility, users, data and SSH identity are not
changed by TLS configuration. Rebuild/replace the backup sidecar's management
image along with this update so restore and upgrade commands understand TLS.

Check from outside the LAN, and again at home:

```sh
curl -I http://YOUR_HOSTNAME/
curl -I https://YOUR_HOSTNAME/
```

Require an HTTP redirect to https://YOUR_HOSTNAME/ and a valid certificate chain
and hostname, without -k. Verify login, notifications/WebSockets, HTTPS clone/push,
SSH clone/push, and representative artifact and LFS transfers. For a registry,
check its /v2 endpoint through the public URL as well.

Readiness connects to the local published TLS port with the public hostname as
SNI and Host, verifying certificate trust and hostname. It therefore checks this
instance rather than another server accidentally reached through DNS. Public DNS
and router reachability are separate acceptance checks. Certificate issuance still
needs incoming public ACME challenges; inspect Gitea logs, DNS and forwarding if
readiness reports a TLS handshake/certificate error.

## Persistence, renewal and restore

Gitea's ACME manager issues and renews certificates. Its account/certificate cache
is inside the persistent data volume, not the container filesystem. Existing dump
backups include that directory, so cached keys/certificates survive container
recreation and backup/restore. After applying TLS, take another verified backup
and exercise a restore into isolated fresh volumes. The destination's settings
choose protocol, canonical URL and CA; restored certificate files are not erased.
For a different hostname, issuance requires that hostname's DNS and reachability.

Acceptance includes recreating the server and checking the certificate serial and
validity period remain unchanged rather than triggering a fresh order. Verify
renewal behavior against the exact deployed Gitea release and its autocert manager.
Use a disposable controlled environment/short-lived compatible test CA where
feasible, or observe actual renewal ahead of expiry; do not change the production
clock or delete its certificate cache to simulate expiry.

## Return to local HTTP

Set tlsEnabled to false, source compose-env.sh again and run podman-compose up -d.
The TLS override is removed, ports return to httpPort/sshPort, and the base file
explicitly disables ACME and HTTP redirection and restores PROTOCOL=http. Local
ROOT_URL defaults to http://sshDomain:httpPort/; optional rootUrl can preserve a
specific legacy canonical URL (for example behind an existing external proxy).
Cached certificates remain in the data volume. Remove/disable router forwards
when returning to local-only operation; this project does not manage your router.

Live ACME/DNS/router tests are deferred until deployment values are supplied.
The implementation workspace lacks Python and a usable rootless Podman service;
run the unit suite and provider merge checks on primary2 or raspberrypi before
applying the feature to the running instance.
