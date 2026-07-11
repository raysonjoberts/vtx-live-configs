#!/usr/bin/env python3
# ip_discover.py — reverse DNS (nslookup) + single ping + CSV output
# CSV columns: IP,name,reachable

import argparse
import csv
import ipaddress
import platform
import re
import shutil
import socket
import subprocess
import sys
import time
from datetime import datetime
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Optional

# Optional dnspython fallback (not required)
try:
    import dns.resolver  # type: ignore
    import dns.reversename  # type: ignore
    import dns.exception  # type: ignore
    HAVE_DNSPY = True
except Exception:
    HAVE_DNSPY = False

ARPA_SUFFIXES = (".in-addr.arpa", ".ip6.arpa")

def _is_hostname(tok: str) -> bool:
    t = tok.strip().rstrip(".")
    if not t or t.lower().endswith(ARPA_SUFFIXES):
        return False
    if re.fullmatch(r"[0-9.]+", t):  # pure IPv4
        return False
    return ("." in t) and re.search(r"[A-Za-z]", t) is not None

def _parse_nslookup_output(text: str, server_host: Optional[str] = None) -> Optional[str]:
    """
    Parse only explicit PTR/Name lines. Ignore 'Server:' and NXDOMAIN messages.
    Never guess using a generic FQDN fallback.
    """
    # Common NXDOMAIN / no-answer signals
    if re.search(r"can't\s+find\s+.*:\s*Non-existent\s+domain", text, re.IGNORECASE):
        return None
    if re.search(r"\bNXDOMAIN\b", text, re.IGNORECASE):
        pass
    if re.search(r"\bNo\s+answer\b", text, re.IGNORECASE):
        pass
    if re.search(r"\bSERVFAIL\b", text, re.IGNORECASE):
        pass

    # Unix-like: "... name = host.domain.com."
    m = re.search(r"\bname\s*=\s*([^\s]+)\.?", text, re.IGNORECASE)
    if m:
        cand = m.group(1).rstrip(".")
        if _is_hostname(cand) and (not server_host or cand.lower() != server_host.lower()):
            return cand

    # Windows/mac: "Name:    host.domain.com"  (ignore "Server:" lines)
    for m in re.finditer(r"^\s*Name:\s*(.+)$", text, re.IGNORECASE | re.MULTILINE):
        cand = m.group(1).strip().rstrip(".")
        if _is_hostname(cand) and (not server_host or cand.lower() != server_host.lower()):
            return cand

    # PTR detail lines: "PTR record = host.domain.com"
    m = re.search(r"\bPTR\s+record\s*=?\s*([^\s]+)\.?", text, re.IGNORECASE)
    if m:
        cand = m.group(1).rstrip(".")
        if _is_hostname(cand) and (not server_host or cand.lower() != server_host.lower()):
            return cand

    return None

def ptr_via_nslookup(ip_str: str, dns_server: Optional[str], timeout: float) -> Optional[str]:
    if not shutil.which("nslookup"):
        return None

    def run_and_parse(cmd):
        try:
            proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
            out = (proc.stdout or "") + "\n" + (proc.stderr or "")
            # Extract the "Server:" hostname to avoid misreporting it as the answer
            srv = None
            ms = re.search(r"^\s*Server:\s*(.+)$", out, re.IGNORECASE | re.MULTILINE)
            if ms:
                srv = ms.group(1).strip().rstrip(".")
            return _parse_nslookup_output(out, server_host=srv)
        except subprocess.TimeoutExpired:
            return None
        except Exception:
            return None

    # Try explicit PTR first
    cmd1 = ["nslookup", "-type=PTR", ip_str]
    if dns_server:
        cmd1.append(dns_server)
    name = run_and_parse(cmd1)
    if name:
        return name

    # Fallback: plain nslookup <ip> [server]
    cmd2 = ["nslookup", ip_str]
    if dns_server:
        cmd2.append(dns_server)
    return run_and_parse(cmd2)

def ptr_via_dnspython(ip_str: str, dns_server: Optional[str], timeout: float) -> Optional[str]:
    if not HAVE_DNSPY:
        return None
    try:
        rev = dns.reversename.from_address(ip_str)  # type: ignore
        r = dns.resolver.Resolver(configure=False)  # type: ignore
        if dns_server:
            r.nameservers = [dns_server]
        r.timeout = timeout
        r.lifetime = timeout
        answers = r.resolve(rev, "PTR")  # type: ignore
        name = answers[0].to_text().rstrip(".")
        return name if _is_hostname(name) else None
    except Exception:
        return None

def ptr_via_socket(ip_str: str) -> Optional[str]:
    try:
        name, _, _ = socket.gethostbyaddr(ip_str)
        name = name.rstrip(".")
        return name if _is_hostname(name) else None
    except Exception:
        return None

def do_ptr(ip_str: str, dns_server: Optional[str], timeout: float, retries: int = 1) -> Optional[str]:
    # nslookup (with optional retries)
    for _ in range(retries + 1):
        name = ptr_via_nslookup(ip_str, dns_server, timeout)
        if name:
            return name
    # fallbacks
    name = ptr_via_dnspython(ip_str, dns_server, timeout)
    if name:
        return name
    return ptr_via_socket(ip_str)

def ping_once(ip_str: str, ping_timeout: float) -> bool:
    system = platform.system().lower()
    cmd = ["ping", "-n", "1", ip_str] if system == "windows" else ["ping", "-c", "1", ip_str]
    try:
        res = subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=ping_timeout)
        return res.returncode == 0
    except subprocess.TimeoutExpired:
        return False
    except Exception:
        return False

def worker(ip, args):
    ip_str = str(ip)

    # Default when not resolved:
    name = "Not Resolved"
    if not args.no_dns:
        ptr = do_ptr(ip_str, args.dns, args.dns_timeout, retries=args.dns_retries)
        if ptr:
            name = ptr

    reachable = "na" if not args.ping else ("yes" if ping_once(ip_str, args.ping_timeout) else "no")
    return {"IP": ip_str, "name": name, "reachable": reachable}

def main():
    ap = argparse.ArgumentParser(description="Reverse DNS (nslookup) + single ping sweep, CSV output.")
    ap.add_argument("-n", "--network", required=True, help="CIDR, e.g. 10.210.86.0/27")
    ap.add_argument("-d", "--dns", default=None, help="DNS server IP for nslookup (optional).")
    ap.add_argument("--no-dns", action="store_true", help="Skip PTR lookups.")
    ap.add_argument("--ping", dest="ping", action="store_true", help="Enable ping (default).")
    ap.add_argument("--no-ping", dest="ping", action="store_false", help="Disable ping.")
    ap.set_defaults(ping=True)
    ap.add_argument("--delay", type=float, default=0.1, help="Delay between sequential queries (sec).")
    ap.add_argument("--out", default="ip_discover_results.csv", help="Output CSV path.")
    ap.add_argument("--parallel", type=int, default=0, help="Threads (0 = sequential).")
    ap.add_argument("--dns-timeout", type=float, default=5.0, help="Timeout for nslookup/dns (sec).")
    ap.add_argument("--dns-retries", type=int, default=1, help="Extra nslookup attempts per IP (default 1).")
    ap.add_argument("--ping-timeout", type=float, default=2.0, help="Ping timeout (sec).")
    args = ap.parse_args()

    try:
        net = ipaddress.ip_network(args.network, strict=False)
    except Exception as e:
        print("Invalid network:", e)
        sys.exit(2)

    ips = list(net.hosts()) if net.num_addresses > 1 else list(net)
    total = len(ips)
    print(f"{datetime.utcnow().isoformat()}Z - Sweep {net} ({total} IPs) "
          f"DNS={args.dns or '(system)'} ping={args.ping} delay={args.delay}s parallel={args.parallel}")

    with open(args.out, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=["IP", "name", "reachable"])
        writer.writeheader()

        if args.parallel and args.parallel > 1:
            with ThreadPoolExecutor(max_workers=args.parallel) as ex:
                futures = {ex.submit(worker, ip, args): ip for ip in ips}
                done = 0
                for fut in as_completed(futures):
                    row = fut.result()
                    writer.writerow(row)
                    done += 1
                    if done % 50 == 0 or done == total:
                        print(f"[{done}/{total}] {row['IP']} -> {row['name']}, {row['reachable']}")
        else:
            done = 0
            for ip in ips:
                start = time.time()
                row = worker(ip, args)
                writer.writerow(row)
                done += 1
                print(f"[{done}/{total}] {row['IP']} -> {row['name']}, {row['reachable']}")
                sleep_for = args.delay - (time.time() - start)
                if sleep_for > 0:
                    time.sleep(sleep_for)

    print(f"Done. Wrote: {args.out}")

if __name__ == "__main__":
    main()
