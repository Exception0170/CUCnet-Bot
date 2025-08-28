import sqlite3
import subprocess
import logging
import ipaddress
import os
import tempfile
from config import WG_SERVER_IP, WG_SERVER_PORT, WG_SERVER_PUBLIC_KEY

logger = logging.getLogger(__name__)

# WireGuard configuration paths
WG_DIR = "/etc/wireguard/"
WG_CONFIG = os.path.join(WG_DIR, "wg0.conf")

def check_wg_config_exists():
    """Check if WireGuard config exists using sudo"""
    try:
        result = subprocess.run(['sudo', 'test', '-f', WG_CONFIG], capture_output=True)
        return result.returncode == 0
    except Exception as e:
        logger.warning(f"Error checking if config exists: {e}")
        return False

def get_next_ip(profile_type):
    """Get the next available IP address for the given profile type"""
    conn = sqlite3.connect('users.db')
    c = conn.cursor()
    
    if profile_type == 'website':
        networks = []
        for third_octet in range(10, 26):
            network_str = f'10.8.{third_octet}.0/24'
            networks.append(ipaddress.ip_network(network_str))
    else:  # personal
        networks = []
        for third_octet in range(100, 256):
            network_str = f'10.8.{third_octet}.0/24'
            networks.append(ipaddress.ip_network(network_str))
    
    # Get only ACTIVE IPs from database
    c.execute('SELECT wg_ip_address FROM profiles WHERE is_active = 1')
    used_ips = {row[0] for row in c.fetchall()}
    
    # Also check IPs currently in WireGuard config (regardless of database status)
    if check_wg_config_exists():
        try:
            result = subprocess.run(['sudo', 'cat', WG_CONFIG], capture_output=True, text=True, check=True)
            for line in result.stdout.split('\n'):
                if line.strip().startswith('AllowedIPs'):
                    ip = line.split('=')[1].strip().split('/')[0]
                    used_ips.add(ip)
        except subprocess.CalledProcessError as e:
            logger.warning(f"Could not read WireGuard config: {e}")
    
    # Convert to ipaddress objects for easier comparison
    used_ip_objects = set()
    for ip_str in used_ips:
        try:
            used_ip_objects.add(ipaddress.ip_address(ip_str))
        except ValueError:
            continue
    
    # Find the first available IP in the specified ranges
    for network in networks:
        for ip in network.hosts():
            if ip not in used_ip_objects:
                # Double-check this IP isn't in use by ANY record in the database
                # (including inactive ones, to avoid potential conflicts)
                c.execute('SELECT COUNT(*) FROM profiles WHERE wg_ip_address = ? AND is_active = 1', (str(ip),))
                if c.fetchone()[0] == 0:
                    conn.close()
                    return str(ip)
    
    conn.close()
    return None

def generate_wireguard_config(profile_name, profile_type, private_key, ip_address):
    """Generate WireGuard client configuration"""
    config = f"""[Interface]
Address = {ip_address}/24
PrivateKey = {private_key}
DNS = 10.8.0.1

[Peer]
PublicKey = {WG_SERVER_PUBLIC_KEY}
Endpoint = {WG_SERVER_IP}:{WG_SERVER_PORT}
AllowedIPs = 10.8.0.0/16
PersistentKeepalive = 25
"""
    return config

def add_peer_to_server(public_key, ip_address, profile_name):
    """Add peer to WireGuard server configuration using sudo"""
    try:
        # Check if config exists first
        if not check_wg_config_exists():
            logger.error("WireGuard config file does not exist")
            return False
        
        # Use sudo to append to the main config
        peer_config = f"\n# Profile: {profile_name}\n[Peer]\nPublicKey = {public_key}\nAllowedIPs = {ip_address}/32\n"
        
        subprocess.run(['sudo', 'bash', '-c', f'echo "{peer_config}" >> {WG_CONFIG}'], check=True)
        
        # Reload WireGuard configuration using sudo
        subprocess.run(['sudo', 'bash', '-c', 'wg addconf wg0 <(wg-quick strip wg0)'], check=True)
        
        logger.info(f"Added peer {ip_address} to WireGuard config")
        return True
        
    except subprocess.CalledProcessError as e:
        logger.error(f"Failed to add peer to server: {e}")
        return False
    except Exception as e:
        logger.error(f"Failed to add peer to server: {e}")
        return False

def remove_peer_from_server(public_key):
    """Remove peer from WireGuard server configuration using sudo"""
    try:
        # Check if config exists first
        if not check_wg_config_exists():
            logger.error("WireGuard config file does not exist")
            return False
        
        # First, remove the peer from the running WireGuard interface
        try:
            subprocess.run(['sudo', 'wg', 'set', 'wg0', 'peer', public_key, 'remove'], 
                         capture_output=True, check=True)
            logger.info(f"Removed peer {public_key} from running WireGuard interface")
        except subprocess.CalledProcessError as e:
            # It's possible the peer wasn't active, so we continue with config removal
            logger.warning(f"Peer might not be active in running interface: {e}")
        
        # Read current config with sudo
        result = subprocess.run(['sudo', 'cat', WG_CONFIG], capture_output=True, text=True, check=True)
        lines = result.stdout.split('\n')
        
        # Filter out the peer
        new_lines = []
        in_peer_section = False
        peer_removed = False
        current_peer_lines = []
        skip_section = False
        
        for line in lines:
            if line.strip().startswith('[Peer]'):
                if in_peer_section:
                    # End of previous peer section
                    if not skip_section:
                        new_lines.extend(current_peer_lines)
                    current_peer_lines = []
                    skip_section = False
                
                in_peer_section = True
                current_peer_lines.append(line + '\n')
                # Check if this is the peer we want to remove
                skip_section = False
                
            elif in_peer_section:
                current_peer_lines.append(line + '\n')
                if line.strip().startswith('PublicKey') and public_key in line:
                    skip_section = True
                    peer_removed = True
                
                # Check for end of section (empty line or new section)
                if line.strip() == '' or (line.strip().startswith('[') and not line.strip().startswith('[Peer]')):
                    if not skip_section:
                        new_lines.extend(current_peer_lines)
                    in_peer_section = False
                    current_peer_lines = []
                    skip_section = False
                    if line.strip() != '':
                        new_lines.append(line + '\n')
            else:
                new_lines.append(line + '\n')
        
        # Handle the last section if we're still in a peer section
        if in_peer_section:
            if not skip_section:
                new_lines.extend(current_peer_lines)
        
        if peer_removed:
            # Create temporary file with new content
            with tempfile.NamedTemporaryFile(mode='w', delete=False) as temp_file:
                temp_file.writelines(new_lines)
                temp_path = temp_file.name
            
            # Replace the config file using sudo
            subprocess.run(['sudo', 'cp', temp_path, WG_CONFIG], check=True)
            subprocess.run(['sudo', 'chmod', '600', WG_CONFIG], check=True)
            
            # Clean up temp file
            os.unlink(temp_path)
            
            logger.info(f"Removed peer with public key {public_key} from config file")
            return True
        else:
            logger.warning(f"Peer with public key {public_key} not found in config file")
            return False
            
    except subprocess.CalledProcessError as e:
        logger.error(f"Failed to modify WireGuard config: {e}")
        return False
    except Exception as e:
        logger.error(f"Failed to remove peer: {e}")
        return False

def generate_keys():
    """Generate WireGuard key pair using sudo"""
    try:
        private_key = subprocess.run(['sudo', 'wg', 'genkey'], capture_output=True, text=True, check=True).stdout.strip()
        public_key = subprocess.run(['sudo', 'wg', 'pubkey'], input=private_key, capture_output=True, text=True, check=True).stdout.strip()
        return private_key, public_key
    except subprocess.CalledProcessError as e:
        logger.error(f"Failed to generate keys: {e}")
        return None, None

def get_server_public_key():
    """Get server's public key using sudo"""
    try:
        result = subprocess.run(['sudo', 'cat', '/etc/wireguard/server-public.key'], 
                              capture_output=True, text=True, check=True)
        return result.stdout.strip()
    except subprocess.CalledProcessError as e:
        logger.error(f"Failed to read server public key: {e}")
        return None