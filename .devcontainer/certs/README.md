Corporate certificate setup (generic)

Use this when your company performs TLS inspection and you need internal root or intermediate CAs trusted in the dev container.

Auto-import source folder in this repository:
- .devcontainer/certs

Accepted file extensions:
- .crt
- .pem

During container creation, .devcontainer/post-create.sh copies cert files from .devcontainer/certs into /usr/local/share/ca-certificates/ and runs update-ca-certificates so curl, git, pip, python requests, and uv trust corporate TLS certificates.

macOS: find and export corporate CA certs

GUI method:
1. Open Keychain Access.
2. Check login and System keychains.
3. Search for your company name, proxy product name, or inspection CA naming used by your org.
4. Export root and any intermediate CA certificates.

CLI method (examples):
1. Discover candidate certs by keyword in System keychain:
	security find-certificate -a -c "Your Company" -Z /Library/Keychains/System.keychain
2. Export matching cert as PEM/CRT:
	security find-certificate -a -p -c "Your Company Root CA" /Library/Keychains/System.keychain > ~/Downloads/company-root-ca.crt
3. If your cert is in login keychain, replace keychain path with:
	~/Library/Keychains/login.keychain-db

Windows: find and export corporate CA certs

GUI method:
1. Run certmgr.msc for Current User certs.
2. Run certlm.msc for Local Machine certs.
3. Check Trusted Root Certification Authorities and Intermediate Certification Authorities.
4. Export relevant corporate CA certs as Base-64 encoded X.509 (.CER).
5. Rename exported .cer files to .crt if needed.

PowerShell method (example by keyword):
1. Find candidates in Local Machine Root store:
	Get-ChildItem Cert:\LocalMachine\Root | Where-Object { $_.Subject -match "Your Company|Inspection|Proxy" } | Select-Object Subject, Thumbprint
2. Export by thumbprint:
	Export-Certificate -Cert "Cert:\LocalMachine\Root\<THUMBPRINT>" -FilePath "$HOME\Downloads\company-root-ca.cer"
3. Rename to .crt (optional):
	Rename-Item "$HOME\Downloads\company-root-ca.cer" "company-root-ca.crt"

Linux: find and export corporate CA certs

Common locations to check:
- /usr/local/share/ca-certificates
- /etc/ssl/certs
- Enterprise-managed trust stores used by your distro or endpoint tooling

Examples:
1. List likely corporate cert files:
	sudo find /usr/local/share/ca-certificates /etc/ssl/certs -type f \( -name "*.crt" -o -name "*.pem" \) | grep -Ei "company|corp|proxy|inspect|ca"
2. Copy selected cert to your home download area:
	cp /path/to/company-root-ca.crt ~/Downloads/

Copy certs into the repository cert folder

From the repository root on macOS or Linux:
1. mkdir -p .devcontainer/certs
2. cp ~/Downloads/*company*ca*.crt .devcontainer/certs/

From the repository root in Windows PowerShell:
1. New-Item -ItemType Directory -Force ".devcontainer\certs" | Out-Null
2. Copy-Item "$HOME\Downloads\company-root-ca.crt" ".devcontainer\certs\"

Rebuild and verify

After placing certs, rebuild the dev container. Then verify inside the container:
1. ls -la /usr/local/share/ca-certificates | grep -Ei "company|corp|proxy|inspect|ca"
2. ls -la /etc/ssl/certs | grep -Ei "company|corp|proxy|inspect|ca"
