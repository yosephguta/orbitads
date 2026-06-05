import sqlite3
conn = sqlite3.connect('/home/ubuntu/orbitads/backend/orbitads.db')
try:
    conn.execute('ALTER TABLE jobs ADD COLUMN outro_video_id INTEGER REFERENCES outro_videos(id)')
    conn.commit()
    print('Done - column added')
except Exception as e:
    print(f'Error: {e}')
conn.close()
