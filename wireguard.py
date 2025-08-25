import sqlite3
import subprocess
import logging
import ipaddress
import os
from config import WG_SERVER_IP, WG_SERVER_PORT, WG_SERVER_PUBLIC_KEY

logger = logging.getLogger(__name__)

# WireGuard configuration paths
WG_DIR = "/etc/wireguard/"
WG_CONFIG = os.path.join(WG_DIR, "wg0.conf")

def get_next_ip(profile_type):
    """Get the next available IP address for the given profile type"""
    conn = sqlite3.connect('users.db')
    c = conn.cursor()
    
    if profile_type == 'website':
        # Website range: 10.8.10.1 to 10.8.25.254
        networks = []
        
        # Create all /24 networks from 10.8.10.0 to 10.8.25.0
        for third_octet in range(10, 26):  # 10 to 25 inclusive
            network_str = f'10.8.{third_octet}.0/24'
            networks.append(ipaddress.ip_network(network_str))
    else:  # personal
        # Personal range: 10.8.100.1 to 10.8.255.254
        networks = []
        
        # Create all /24 networks from 10.8.100.0 to 10.8.255.0
        for third_octet in range(100, 256):  # 100 to 255 inclusive
            network_str = f'10.8.{third_octet}.0/24'
            networks.append(ipaddress.ip_network(network_str))
    
    # Get all used IPs from database
    c.execute('SELECT wg_ip_address FROM profiles WHERE is_active = 1')
    used_ips = {row[0] for row in c.fetchall()}
    
    # Also check IPs currently in WireGuard config
    try:
        if os.path.exists(WG_CONFIG):
            with open(WG_CONFIG, 'r') as f:
                for line in f:
                    if line.strip().startswith('AllowedIPs'):
                        ip = line.split('=')[1].strip().split('/')[0]
                        used_ips.add(ip)
    except Exception as e:
        logger.warning(f"Could not read WireGuard config: {e}")
    
    # Convert to ipaddress objects for easier comparison
    used_ip_objects = set()
    for ip_str in used_ips:
        try:
            used_ip_objects.add(ipaddress.ip_address(ip_str))
        except ValueError:
            continue  # Skip invalid IPs
    
    # Find the first available IP in the specified ranges
    for network in networks:
        # Skip network and broadcast addresses
        for ip in network.hosts():
            if ip not in used_ip_objects:
                # Double-check this IP isn't in use by querying the database again
                c.execute('SELECT COUNT(*) FROM profiles WHERE wg_ip_address = ?', (str(ip),))
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
    """Add peer to WireGuard server configuration"""
    try:
        # Read current config
        if not os.path.exists(WG_CONFIG):
            logger.error("WireGuard config file not found")
            return False
        
        # Add the new peer section
        peer_config = f"""
# Profile: {profile_name}
[Peer]
PublicKey = {public_key}
AllowedIPs = {ip_address}/32
"""
        
        # Append to config file
        with open(WG_CONFIG, 'a') as f:
            f.write(peer_config)
        
        # Reload WireGuard configuration
        subprocess.run(['sudo', 'wg', 'addconf', 'wg0', '<(/usr/bin/wg-quick strip wg0)'], 
                      shell=True, executable='/bin/bash', check=True)
        
        logger.info(f"Added peer {ip_address} to WireGuard config")
        return True
        
    except subprocess.CalledProcessError as e:
        logger.error(f"Failed to reload WireGuard config: {e}")
        return False
    except Exception as e:
        logger.error(f"Failed to add peer to server: {e}")
        return False

def remove_peer_from_server(public_key):
    """Remove peer from WireGuard server configuration by public key"""
    try:
        if not os.path.exists(WG_CONFIG):
            logger.error("WireGuard config file not found")
            return False

        # Read current config and filter out the peer
        new_lines = []
        in_peer_section = False
        peer_removed = False
        
        with open(WG_CONFIG, 'r') as f:
            for line in f:
                if line.strip().startswith('[Peer]'):
                    # Start of a new peer section
                    in_peer_section = True
                    current_peer_lines = [line]
                elif in_peer_section:
                    current_peer_lines.append(line)
                    if line.strip().startswith('PublicKey') and public_key in line:
                        # This is the peer we want to remove, skip adding these lines
                        in_peer_section = False
                        peer_removed = True
                        continue
                    if line.strip() == '' or line.strip().startswith('['):
                        # End of peer section, add all lines if not the target peer
                        if not any(public_key in l for l in current_peer_lines):
                            new_lines.extend(current_peer_lines)
                        in_peer_section = False
                else:
                    new_lines.append(line)
        
        # Write updated config
        if peer_removed:
            with open(WG_CONFIG, 'w') as f:
                f.writelines(new_lines)
            
            # Reload WireGuard configuration
            subprocess.run(['sudo', 'wg', 'addconf', 'wg0', '<(/usr/bin/wg-quick strip wg0)'], 
                          shell=True, executable='/bin/bash', check=True)
            
            logger.info(f"Removed peer with public key {public_key} from WireGuard config")
            return True
        else:
            logger.warning(f"Peer with public key {public_key} not found in config")
            return False
            
    except subprocess.CalledProcessError as e:
        logger.error(f"Failed to reload WireGuard config: {e}")
        return False
    except Exception as e:
        logger.error(f"Failed to remove peer from server: {e}")
        return False

def generate_keys():
    """Generate WireGuard key pair"""
    try:
        private_key = subprocess.run(['wg', 'genkey'], capture_output=True, text=True, check=True).stdout.strip()
        public_key = subprocess.run(['wg', 'pubkey'], input=private_key, capture_output=True, text=True, check=True).stdout.strip()
        return private_key, public_key
    except subprocess.CalledProcessError as e:
        logger.error(f"Failed to generate keys: {e}")
        return None, None