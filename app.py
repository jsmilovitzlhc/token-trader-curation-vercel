"""
Token Trader Link Curation Tool

A simple web app for collecting and curating links for the newsletter.
Vercel + Postgres version.
"""

import os
import re
import json
import requests
from datetime import datetime
from functools import wraps
from urllib.parse import urlparse

import psycopg2
from psycopg2.extras import RealDictCursor

from flask import Flask, render_template, request, redirect, url_for, jsonify, session, g

app = Flask(__name__)
app.secret_key = os.environ.get('SECRET_KEY', 'token-trader-dev-key-change-in-prod')

# Config
DATABASE_URL = os.environ.get('POSTGRES_URL', os.environ.get('DATABASE_URL', ''))
PASSWORD = os.environ.get('CURATION_PASSWORD', 'TOKENTRADER')

# Categories for Token Trader
CATEGORIES = [
    'Inference Pricing',
    'GPU Markets',
    'Model Economics',
    'Cloud Providers',
    'Open Source Models',
    'Enterprise AI',
    'Regulatory',
    'Market Analysis',
    'Compute Futures',
    'Data Centers',
    'Chip Supply Chain',
    'Other',
]

STATUSES = ['saved', 'scheduled', 'published', 'archived']


def get_db():
    """Get database connection."""
    if 'db' not in g:
        # Handle Vercel's postgres:// vs postgresql:// URL format
        db_url = DATABASE_URL
        if db_url.startswith('postgres://'):
            db_url = db_url.replace('postgres://', 'postgresql://', 1)
        g.db = psycopg2.connect(db_url, cursor_factory=RealDictCursor)
    return g.db


@app.teardown_appcontext
def close_db(exception):
    db = g.pop('db', None)
    if db is not None:
        db.close()


def init_db():
    """Initialize database schema."""
    if not DATABASE_URL:
        return
    db = get_db()
    cur = db.cursor()
    cur.execute('''
        CREATE TABLE IF NOT EXISTS links (
            id SERIAL PRIMARY KEY,
            url TEXT NOT NULL UNIQUE,
            title TEXT,
            description TEXT,
            source TEXT,
            author TEXT,
            image_url TEXT,
            category TEXT DEFAULT 'Other',
            tags TEXT DEFAULT '',
            notes TEXT DEFAULT '',
            status TEXT DEFAULT 'saved',
            issue TEXT DEFAULT '',
            priority INTEGER DEFAULT 0,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    ''')
    db.commit()
    cur.close()


def require_auth(f):
    """Simple password authentication decorator."""
    @wraps(f)
    def decorated(*args, **kwargs):
        if not session.get('authenticated'):
            return redirect(url_for('login'))
        return f(*args, **kwargs)
    return decorated


def fetch_metadata(url):
    """Fetch Open Graph and meta data from URL."""
    try:
        headers = {
            'User-Agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36'
        }
        response = requests.get(url, headers=headers, timeout=10)
        html = response.text

        # Extract Open Graph tags
        og_title = re.search(r'<meta[^>]*property=["\']og:title["\'][^>]*content=["\']([^"\']+)["\']', html, re.I)
        og_desc = re.search(r'<meta[^>]*property=["\']og:description["\'][^>]*content=["\']([^"\']+)["\']', html, re.I)
        og_image = re.search(r'<meta[^>]*property=["\']og:image["\'][^>]*content=["\']([^"\']+)["\']', html, re.I)
        og_site = re.search(r'<meta[^>]*property=["\']og:site_name["\'][^>]*content=["\']([^"\']+)["\']', html, re.I)

        # Fallback to regular meta tags
        if not og_title:
            og_title = re.search(r'<title[^>]*>([^<]+)</title>', html, re.I)
        if not og_desc:
            og_desc = re.search(r'<meta[^>]*name=["\']description["\'][^>]*content=["\']([^"\']+)["\']', html, re.I)

        # Extract author
        author = re.search(r'<meta[^>]*name=["\']author["\'][^>]*content=["\']([^"\']+)["\']', html, re.I)

        parsed = urlparse(url)
        source = og_site.group(1) if og_site else parsed.netloc

        return {
            'title': og_title.group(1) if og_title else '',
            'description': og_desc.group(1) if og_desc else '',
            'image_url': og_image.group(1) if og_image else '',
            'source': source,
            'author': author.group(1) if author else '',
        }
    except Exception as e:
        print(f"Error fetching metadata: {e}")
        parsed = urlparse(url)
        return {
            'title': '',
            'description': '',
            'image_url': '',
            'source': parsed.netloc,
            'author': '',
        }


@app.route('/login', methods=['GET', 'POST'])
def login():
    """Login page."""
    if request.method == 'POST':
        if request.form.get('password') == PASSWORD:
            session['authenticated'] = True
            return redirect(url_for('index'))
        return render_template('login.html', error='Invalid password')
    return render_template('login.html')


@app.route('/logout')
def logout():
    session.pop('authenticated', None)
    return redirect(url_for('login'))


@app.route('/')
@require_auth
def index():
    """Main dashboard."""
    db = get_db()
    cur = db.cursor()

    # Get filter parameters
    status_filter = request.args.get('status', '')
    category_filter = request.args.get('category', '')
    issue_filter = request.args.get('issue', '')

    query = 'SELECT * FROM links WHERE 1=1'
    params = []

    if status_filter:
        query += ' AND status = %s'
        params.append(status_filter)
    if category_filter:
        query += ' AND category = %s'
        params.append(category_filter)
    if issue_filter:
        query += ' AND issue = %s'
        params.append(issue_filter)

    query += ' ORDER BY priority DESC, created_at DESC'

    cur.execute(query, params)
    links = cur.fetchall()

    # Get unique issues for filter dropdown
    cur.execute("SELECT DISTINCT issue FROM links WHERE issue != '' ORDER BY issue DESC")
    issues = cur.fetchall()

    # Get counts by status
    counts = {}
    for status in STATUSES:
        cur.execute('SELECT COUNT(*) as count FROM links WHERE status = %s', (status,))
        counts[status] = cur.fetchone()['count']

    cur.close()

    return render_template('index.html',
                          links=links,
                          categories=CATEGORIES,
                          statuses=STATUSES,
                          issues=[i['issue'] for i in issues],
                          counts=counts,
                          filters={
                              'status': status_filter,
                              'category': category_filter,
                              'issue': issue_filter,
                          })


@app.route('/add', methods=['GET', 'POST'])
@require_auth
def add_link():
    """Add a new link."""
    if request.method == 'POST':
        url = request.form.get('url', '').strip()

        if not url:
            return render_template('add.html', categories=CATEGORIES, error='URL is required')

        # Check for duplicate
        db = get_db()
        cur = db.cursor()
        cur.execute('SELECT id FROM links WHERE url = %s', (url,))
        existing = cur.fetchone()
        if existing:
            cur.close()
            return redirect(url_for('edit_link', link_id=existing['id']))

        # Fetch metadata
        metadata = fetch_metadata(url)

        # Get form data (override metadata if provided)
        title = request.form.get('title', '').strip() or metadata['title']
        description = request.form.get('description', '').strip() or metadata['description']
        source = request.form.get('source', '').strip() or metadata['source']
        author = request.form.get('author', '').strip() or metadata['author']
        category = request.form.get('category', 'Other')
        tags = request.form.get('tags', '').strip()
        notes = request.form.get('notes', '').strip()
        issue = request.form.get('issue', '').strip()
        priority = int(request.form.get('priority', 0))

        cur.execute('''
            INSERT INTO links (url, title, description, source, author, image_url, category, tags, notes, issue, priority)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        ''', (url, title, description, source, author, metadata['image_url'], category, tags, notes, issue, priority))
        db.commit()
        cur.close()

        return redirect(url_for('index'))

    return render_template('add.html', categories=CATEGORIES)


@app.route('/fetch-metadata', methods=['POST'])
@require_auth
def fetch_metadata_api():
    """API endpoint to fetch metadata for a URL."""
    url = request.json.get('url', '')
    if not url:
        return jsonify({'error': 'URL required'}), 400

    metadata = fetch_metadata(url)
    return jsonify(metadata)


@app.route('/edit/<int:link_id>', methods=['GET', 'POST'])
@require_auth
def edit_link(link_id):
    """Edit an existing link."""
    db = get_db()
    cur = db.cursor()
    cur.execute('SELECT * FROM links WHERE id = %s', (link_id,))
    link = cur.fetchone()

    if not link:
        cur.close()
        return redirect(url_for('index'))

    if request.method == 'POST':
        title = request.form.get('title', '').strip()
        description = request.form.get('description', '').strip()
        source = request.form.get('source', '').strip()
        author = request.form.get('author', '').strip()
        category = request.form.get('category', 'Other')
        tags = request.form.get('tags', '').strip()
        notes = request.form.get('notes', '').strip()
        status = request.form.get('status', 'saved')
        issue = request.form.get('issue', '').strip()
        priority = int(request.form.get('priority', 0))

        cur.execute('''
            UPDATE links SET
                title = %s, description = %s, source = %s, author = %s,
                category = %s, tags = %s, notes = %s, status = %s, issue = %s,
                priority = %s, updated_at = CURRENT_TIMESTAMP
            WHERE id = %s
        ''', (title, description, source, author, category, tags, notes, status, issue, priority, link_id))
        db.commit()
        cur.close()

        return redirect(url_for('index'))

    cur.close()
    return render_template('edit.html', link=link, categories=CATEGORIES, statuses=STATUSES)


@app.route('/delete/<int:link_id>', methods=['POST'])
@require_auth
def delete_link(link_id):
    """Delete a link."""
    db = get_db()
    cur = db.cursor()
    cur.execute('DELETE FROM links WHERE id = %s', (link_id,))
    db.commit()
    cur.close()
    return redirect(url_for('index'))


@app.route('/bulk-update', methods=['POST'])
@require_auth
def bulk_update():
    """Bulk update links."""
    link_ids = request.form.getlist('link_ids')
    action = request.form.get('action')

    if not link_ids:
        return redirect(url_for('index'))

    db = get_db()
    cur = db.cursor()

    # Convert to integers
    link_ids = [int(lid) for lid in link_ids]

    if action in STATUSES:
        cur.execute(
            'UPDATE links SET status = %s, updated_at = CURRENT_TIMESTAMP WHERE id = ANY(%s)',
            (action, link_ids)
        )
    elif action == 'delete':
        cur.execute('DELETE FROM links WHERE id = ANY(%s)', (link_ids,))

    db.commit()
    cur.close()
    return redirect(url_for('index'))


@app.route('/export')
@require_auth
def export_links():
    """Export links for newsletter."""
    db = get_db()
    cur = db.cursor()

    issue = request.args.get('issue', '')
    format_type = request.args.get('format', 'markdown')

    query = "SELECT * FROM links WHERE status = 'scheduled'"
    params = []

    if issue:
        query += ' AND issue = %s'
        params.append(issue)

    query += ' ORDER BY category, priority DESC'

    cur.execute(query, params)
    links = cur.fetchall()
    cur.close()

    # Group by category
    by_category = {}
    for link in links:
        cat = link['category']
        if cat not in by_category:
            by_category[cat] = []
        by_category[cat].append(link)

    if format_type == 'markdown':
        output = f"# Links for Newsletter\n\n"
        if issue:
            output += f"**Issue:** {issue}\n\n"

        for category, cat_links in by_category.items():
            output += f"## {category}\n\n"
            for link in cat_links:
                output += f"**[{link['title']}]({link['url']})**"
                if link['source']:
                    output += f" — {link['source']}"
                output += "\n"
                if link['description']:
                    output += f"{link['description']}\n"
                if link['notes']:
                    output += f"*{link['notes']}*\n"
                output += "\n"

        return render_template('export.html', output=output, format='markdown', issue=issue)

    elif format_type == 'html':
        output = ""
        for category, cat_links in by_category.items():
            output += f'<h3 style="color: #c9a227; margin-top: 24px;">{category}</h3>\n'
            for link in cat_links:
                output += f'<p style="margin-bottom: 16px;">\n'
                output += f'  <strong><a href="{link["url"]}" style="color: #1a1a1a;">{link["title"]}</a></strong>'
                if link['source']:
                    output += f' — <span style="color: #666;">{link["source"]}</span>'
                output += '<br>\n'
                if link['description']:
                    output += f'  <span style="color: #333;">{link["description"]}</span><br>\n'
                if link['notes']:
                    output += f'  <em style="color: #666;">{link["notes"]}</em>\n'
                output += '</p>\n'

        return render_template('export.html', output=output, format='html', issue=issue)

    elif format_type == 'json':
        data = [dict(link) for link in links]
        return jsonify(data)

    return redirect(url_for('index'))


# Initialize DB on first request
_db_initialized = False

@app.before_request
def before_first_request():
    global _db_initialized
    if not _db_initialized and DATABASE_URL:
        init_db()
        _db_initialized = True


if __name__ == '__main__':
    with app.app_context():
        if DATABASE_URL:
            init_db()
    app.run(debug=True, port=5001)
