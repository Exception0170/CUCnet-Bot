import sqlite3
import logging

# Set up logging
logging.basicConfig(format='%(asctime)s - %(name)s - %(levelname)s - %(message)s', level=logging.INFO)
logger = logging.getLogger(__name__)

DB_PATH = 'users.db'

def init_db():
    """Initialize the database with required tables"""
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    
    # Users table (verified users)
    c.execute('''
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY,
            telegram_id INTEGER UNIQUE NOT NULL,
            telegram_username TEXT,
            is_verified BOOLEAN DEFAULT 0,
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP
        )
    ''')
    
    # Admin requests table (for verification workflow)
    c.execute('''
        CREATE TABLE IF NOT EXISTS admin_requests (
            id INTEGER PRIMARY KEY,
            user_id INTEGER NOT NULL,
            status TEXT DEFAULT 'pending', -- pending, approved, rejected
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (user_id) REFERENCES users (id)
        )
    ''')
    
    # Profiles table
    c.execute('''
        CREATE TABLE IF NOT EXISTS profiles (
            id INTEGER PRIMARY KEY,
            user_id INTEGER NOT NULL,
            profile_name TEXT NOT NULL, -- username-profilename
            profile_type TEXT NOT NULL, -- personal/website
            wg_public_key TEXT UNIQUE NOT NULL,
            wg_private_key TEXT NOT NULL,
            wg_ip_address TEXT UNIQUE NOT NULL,
            is_active BOOLEAN DEFAULT 1,
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (user_id) REFERENCES users (id),
            UNIQUE(user_id, profile_name)
        )
    ''')
    
    conn.commit()
    conn.close()
    logger.info("Database initialized successfully")

# Initialize the database when this module is imported
init_db()