"""
Token Trader Link Curation Tool

A simple web app for collecting and curating links for the newsletter.
Features:
- Manual link curation
- Automated article scraping from RSS feeds and Google News
- Beehiiv newsletter integration
Vercel + Postgres version.
"""

import os
import re
import json
import requests
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta
from functools import wraps
from urllib.parse import urlparse, quote_plus
from html import unescape

import psycopg2
from psycopg2.extras import RealDictCursor

from flask import Flask, render_template, request, redirect, url_for, jsonify, session, g

app = Flask(__name__)
app.secret_key = os.environ.get('SECRET_KEY', 'token-trader-dev-key-change-in-prod')

# Config
DATABASE_URL = os.environ.get('POSTGRES_URL', os.environ.get('DATABASE_URL', ''))
PASSWORD = os.environ.get('CURATION_PASSWORD', 'TOKENTRADER')
BEEHIIV_API_KEY = os.environ.get('BEEHIIV_API_KEY', '')
BEEHIIV_PUBLICATION_ID = os.environ.get('BEEHIIV_PUBLICATION_ID', '')

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

STATUSES = ['scraped', 'saved', 'scheduled', 'published', 'archived']


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

    # Main links table
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
            scraped_from TEXT DEFAULT '',
            published_date TEXT DEFAULT '',
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    ''')

    # Scrape sources table
    cur.execute('''
        CREATE TABLE IF NOT EXISTS scrape_sources (
            id SERIAL PRIMARY KEY,
            name TEXT NOT NULL,
            source_type TEXT NOT NULL,
            url TEXT,
            keywords TEXT,
            category TEXT DEFAULT 'Other',
            enabled BOOLEAN DEFAULT TRUE,
            last_scraped TIMESTAMP,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    ''')

    # Add new columns if they don't exist (for existing installations)
    try:
        cur.execute("ALTER TABLE links ADD COLUMN IF NOT EXISTS scraped_from TEXT DEFAULT ''")
        cur.execute("ALTER TABLE links ADD COLUMN IF NOT EXISTS published_date TEXT DEFAULT ''")
    except:
        pass

    db.commit()
    cur.close()

    # Initialize default scrape sources
    init_default_sources()


def init_default_sources():
    """Initialize default scrape sources for Token Trader topics."""
    db = get_db()
    cur = db.cursor()

    # Check if sources already exist
    cur.execute("SELECT COUNT(*) as count FROM scrape_sources")
    if cur.fetchone()['count'] > 0:
        cur.close()
        return

    # Default RSS feeds and search queries for Token Trader
    default_sources = [
        # RSS Feeds
        ('TechCrunch AI', 'rss', 'https://techcrunch.com/tag/artificial-intelligence/feed/', '', 'Model Economics'),
        ('Ars Technica Tech', 'rss', 'https://feeds.arstechnica.com/arstechnica/technology-lab', '', 'Other'),
        ('The Verge AI', 'rss', 'https://www.theverge.com/rss/ai-artificial-intelligence/index.xml', '', 'Model Economics'),
        ('Reuters Tech', 'rss', 'https://www.reutersagency.com/feed/?taxonomy=best-topics&post_type=best&best-type=reuters-best-technology', '', 'Market Analysis'),
        ('Hacker News', 'rss', 'https://hnrss.org/newest?q=GPU+OR+inference+OR+NVIDIA+OR+OpenAI', '', 'Other'),

        # Google News searches for Token Trader topics
        ('CME Compute Futures', 'google_news', '', 'CME compute futures GPU', 'Compute Futures'),
        ('GPU Pricing News', 'google_news', '', 'GPU pricing NVIDIA H100 rental', 'GPU Markets'),
        ('AI Inference Pricing', 'google_news', '', 'AI inference pricing tokens API', 'Inference Pricing'),
        ('Data Center AI', 'google_news', '', 'data center AI infrastructure', 'Data Centers'),
        ('OpenAI Anthropic Pricing', 'google_news', '', 'OpenAI Anthropic Google AI pricing', 'Inference Pricing'),
        ('Cloud AI Providers', 'google_news', '', 'AWS Azure Google Cloud AI compute', 'Cloud Providers'),
        ('AI Chip Supply', 'google_news', '', 'NVIDIA AMD Intel AI chips supply', 'Chip Supply Chain'),
        ('Compute Marketplace', 'google_news', '', 'CoreWeave Lambda Labs Together AI', 'Cloud Providers'),
        ('AI Regulation', 'google_news', '', 'AI regulation CFTC compute commodity', 'Regulatory'),
    ]

    for name, source_type, url, keywords, category in default_sources:
        try:
            cur.execute('''
                INSERT INTO scrape_sources (name, source_type, url, keywords, category)
                VALUES (%s, %s, %s, %s, %s)
            ''', (name, source_type, url, keywords, category))
        except:
            pass

    db.commit()
    cur.close()


def parse_rss_feed(url):
    """Parse an RSS feed and return articles."""
    articles = []
    try:
        headers = {
            'User-Agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36'
        }
        response = requests.get(url, headers=headers, timeout=15)
        root = ET.fromstring(response.content)

        # Handle different RSS formats
        items = root.findall('.//item') or root.findall('.//{http://www.w3.org/2005/Atom}entry')

        for item in items[:10]:  # Limit to 10 most recent
            # Standard RSS
            title = item.find('title')
            link = item.find('link')
            description = item.find('description')
            pub_date = item.find('pubDate')
            author = item.find('author') or item.find('{http://purl.org/dc/elements/1.1/}creator')

            # Atom format fallback
            if link is None:
                link = item.find('{http://www.w3.org/2005/Atom}link')
                if link is not None:
                    link_href = link.get('href')
                else:
                    continue
            else:
                link_href = link.text

            if title is None:
                title = item.find('{http://www.w3.org/2005/Atom}title')

            title_text = title.text if title is not None else ''
            desc_text = description.text if description is not None else ''

            # Clean HTML from description
            if desc_text:
                desc_text = re.sub(r'<[^>]+>', '', desc_text)
                desc_text = unescape(desc_text)[:500]

            articles.append({
                'url': link_href,
                'title': unescape(title_text) if title_text else '',
                'description': desc_text,
                'published_date': pub_date.text if pub_date is not None else '',
                'author': author.text if author is not None else '',
            })
    except Exception as e:
        print(f"Error parsing RSS feed {url}: {e}")

    return articles


def search_google_news(keywords, days_back=7):
    """Search Google News RSS for keywords."""
    articles = []
    try:
        # Google News RSS search
        encoded_query = quote_plus(keywords)
        url = f"https://news.google.com/rss/search?q={encoded_query}+when:{days_back}d&hl=en-US&gl=US&ceid=US:en"

        headers = {
            'User-Agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36'
        }
        response = requests.get(url, headers=headers, timeout=15)
        root = ET.fromstring(response.content)

        for item in root.findall('.//item')[:8]:  # Limit results
            title = item.find('title')
            link = item.find('link')
            pub_date = item.find('pubDate')
            source = item.find('source')

            if title is None or link is None:
                continue

            # Google News links redirect - try to get actual URL from link
            actual_url = link.text

            articles.append({
                'url': actual_url,
                'title': unescape(title.text) if title.text else '',
                'description': '',
                'published_date': pub_date.text if pub_date is not None else '',
                'source': source.text if source is not None else '',
                'author': '',
            })
    except Exception as e:
        print(f"Error searching Google News for '{keywords}': {e}")

    return articles


def scrape_all_sources():
    """Scrape all enabled sources and return new articles."""
    db = get_db()
    cur = db.cursor()

    cur.execute("SELECT * FROM scrape_sources WHERE enabled = TRUE")
    sources = cur.fetchall()

    new_articles = []
    for source in sources:
        articles = []

        if source['source_type'] == 'rss' and source['url']:
            articles = parse_rss_feed(source['url'])
        elif source['source_type'] == 'google_news' and source['keywords']:
            articles = search_google_news(source['keywords'])

        for article in articles:
            # Check if URL already exists
            cur.execute("SELECT id FROM links WHERE url = %s", (article['url'],))
            if cur.fetchone():
                continue

            # Insert new article
            try:
                cur.execute('''
                    INSERT INTO links (url, title, description, source, author, category, status, scraped_from, published_date)
                    VALUES (%s, %s, %s, %s, %s, %s, 'scraped', %s, %s)
                    RETURNING id
                ''', (
                    article['url'],
                    article.get('title', ''),
                    article.get('description', ''),
                    article.get('source', source['name']),
                    article.get('author', ''),
                    source['category'],
                    source['name'],
                    article.get('published_date', '')
                ))
                new_id = cur.fetchone()['id']
                new_articles.append({**article, 'id': new_id, 'source_name': source['name']})
            except Exception as e:
                print(f"Error inserting article: {e}")
                db.rollback()
                continue

        # Update last_scraped timestamp
        cur.execute(
            "UPDATE scrape_sources SET last_scraped = CURRENT_TIMESTAMP WHERE id = %s",
            (source['id'],)
        )

    db.commit()
    cur.close()
    return new_articles


def publish_to_beehiiv(title, content_html, status='draft'):
    """Publish content to Beehiiv."""
    if not BEEHIIV_API_KEY or not BEEHIIV_PUBLICATION_ID:
        return {'error': 'Beehiiv API credentials not configured'}

    try:
        headers = {
            'Authorization': f'Bearer {BEEHIIV_API_KEY}',
            'Content-Type': 'application/json'
        }

        data = {
            'title': title,
            'content_html': content_html,
            'status': status,  # 'draft', 'confirmed', or 'archived'
        }

        response = requests.post(
            f'https://api.beehiiv.com/v2/publications/{BEEHIIV_PUBLICATION_ID}/posts',
            headers=headers,
            json=data,
            timeout=30
        )

        if response.status_code in [200, 201]:
            return {'success': True, 'data': response.json()}
        else:
            return {'error': f"Beehiiv API error: {response.status_code} - {response.text}"}

    except Exception as e:
        return {'error': f"Error publishing to Beehiiv: {str(e)}"}


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


@app.route('/scrape')
@require_auth
def scrape_page():
    """Scrape management page."""
    db = get_db()
    cur = db.cursor()

    # Get all sources
    cur.execute("SELECT * FROM scrape_sources ORDER BY enabled DESC, name")
    sources = cur.fetchall()

    # Get scraped articles count
    cur.execute("SELECT COUNT(*) as count FROM links WHERE status = 'scraped'")
    scraped_count = cur.fetchone()['count']

    # Get recent scraped articles
    cur.execute("""
        SELECT * FROM links
        WHERE status = 'scraped'
        ORDER BY created_at DESC
        LIMIT 50
    """)
    recent_scraped = cur.fetchall()

    cur.close()

    return render_template('scrape.html',
                          sources=sources,
                          scraped_count=scraped_count,
                          recent_scraped=recent_scraped,
                          categories=CATEGORIES)


@app.route('/scrape/run', methods=['POST'])
@require_auth
def run_scrape():
    """Run scraping on all enabled sources."""
    new_articles = scrape_all_sources()
    return jsonify({
        'success': True,
        'new_articles': len(new_articles),
        'articles': new_articles[:20]  # Return first 20 for display
    })


@app.route('/scrape/source', methods=['POST'])
@require_auth
def add_source():
    """Add a new scrape source."""
    db = get_db()
    cur = db.cursor()

    name = request.form.get('name', '').strip()
    source_type = request.form.get('source_type', 'rss')
    url = request.form.get('url', '').strip()
    keywords = request.form.get('keywords', '').strip()
    category = request.form.get('category', 'Other')

    if not name:
        return redirect(url_for('scrape_page'))

    try:
        cur.execute('''
            INSERT INTO scrape_sources (name, source_type, url, keywords, category)
            VALUES (%s, %s, %s, %s, %s)
        ''', (name, source_type, url, keywords, category))
        db.commit()
    except Exception as e:
        print(f"Error adding source: {e}")

    cur.close()
    return redirect(url_for('scrape_page'))


@app.route('/scrape/source/<int:source_id>/toggle', methods=['POST'])
@require_auth
def toggle_source(source_id):
    """Toggle a source's enabled status."""
    db = get_db()
    cur = db.cursor()
    cur.execute(
        "UPDATE scrape_sources SET enabled = NOT enabled WHERE id = %s",
        (source_id,)
    )
    db.commit()
    cur.close()
    return redirect(url_for('scrape_page'))


@app.route('/scrape/source/<int:source_id>/delete', methods=['POST'])
@require_auth
def delete_source(source_id):
    """Delete a scrape source."""
    db = get_db()
    cur = db.cursor()
    cur.execute("DELETE FROM scrape_sources WHERE id = %s", (source_id,))
    db.commit()
    cur.close()
    return redirect(url_for('scrape_page'))


@app.route('/scrape/approve', methods=['POST'])
@require_auth
def approve_scraped():
    """Move scraped articles to saved status."""
    link_ids = request.form.getlist('link_ids')
    if not link_ids:
        return redirect(url_for('scrape_page'))

    db = get_db()
    cur = db.cursor()
    link_ids = [int(lid) for lid in link_ids]
    cur.execute(
        "UPDATE links SET status = 'saved', updated_at = CURRENT_TIMESTAMP WHERE id = ANY(%s)",
        (link_ids,)
    )
    db.commit()
    cur.close()
    return redirect(url_for('scrape_page'))


@app.route('/scrape/dismiss', methods=['POST'])
@require_auth
def dismiss_scraped():
    """Archive/dismiss scraped articles."""
    link_ids = request.form.getlist('link_ids')
    if not link_ids:
        return redirect(url_for('scrape_page'))

    db = get_db()
    cur = db.cursor()
    link_ids = [int(lid) for lid in link_ids]
    cur.execute("DELETE FROM links WHERE id = ANY(%s)", (link_ids,))
    db.commit()
    cur.close()
    return redirect(url_for('scrape_page'))


@app.route('/beehiiv')
@require_auth
def beehiiv_page():
    """Beehiiv integration page."""
    db = get_db()
    cur = db.cursor()

    issue = request.args.get('issue', '')

    # Get scheduled links
    query = "SELECT * FROM links WHERE status = 'scheduled'"
    params = []
    if issue:
        query += ' AND issue = %s'
        params.append(issue)
    query += ' ORDER BY category, priority DESC'

    cur.execute(query, params)
    links = cur.fetchall()

    # Get unique issues
    cur.execute("SELECT DISTINCT issue FROM links WHERE issue != '' ORDER BY issue DESC")
    issues = [i['issue'] for i in cur.fetchall()]

    cur.close()

    # Group by category
    by_category = {}
    for link in links:
        cat = link['category']
        if cat not in by_category:
            by_category[cat] = []
        by_category[cat].append(link)

    # Generate HTML preview
    html_preview = ""
    for category, cat_links in by_category.items():
        html_preview += f'<h3 style="color: #c9a227; margin-top: 24px; font-family: Georgia, serif;">{category}</h3>\n'
        for link in cat_links:
            html_preview += '<div style="margin-bottom: 16px;">\n'
            html_preview += f'  <strong><a href="{link["url"]}" style="color: #1a1a1a; text-decoration: none;">{link["title"]}</a></strong>'
            if link['source']:
                html_preview += f' <span style="color: #888;">— {link["source"]}</span>'
            html_preview += '<br>\n'
            if link['description']:
                html_preview += f'  <span style="color: #333; font-size: 14px;">{link["description"]}</span><br>\n'
            if link['notes']:
                html_preview += f'  <em style="color: #666; font-size: 13px;">{link["notes"]}</em>\n'
            html_preview += '</div>\n'

    beehiiv_configured = bool(BEEHIIV_API_KEY and BEEHIIV_PUBLICATION_ID)

    return render_template('beehiiv.html',
                          links=links,
                          by_category=by_category,
                          html_preview=html_preview,
                          issues=issues,
                          selected_issue=issue,
                          beehiiv_configured=beehiiv_configured)


@app.route('/beehiiv/publish', methods=['POST'])
@require_auth
def publish_beehiiv():
    """Publish scheduled content to Beehiiv."""
    title = request.form.get('title', 'The Token Trader Newsletter')
    issue = request.form.get('issue', '')

    db = get_db()
    cur = db.cursor()

    # Get scheduled links
    query = "SELECT * FROM links WHERE status = 'scheduled'"
    params = []
    if issue:
        query += ' AND issue = %s'
        params.append(issue)
    query += ' ORDER BY category, priority DESC'

    cur.execute(query, params)
    links = cur.fetchall()
    cur.close()

    if not links:
        return jsonify({'error': 'No scheduled links to publish'})

    # Group by category
    by_category = {}
    for link in links:
        cat = link['category']
        if cat not in by_category:
            by_category[cat] = []
        by_category[cat].append(link)

    # Build HTML content
    html_content = '<div style="font-family: Georgia, serif; max-width: 600px; margin: 0 auto;">\n'

    for category, cat_links in by_category.items():
        html_content += f'<h3 style="color: #c9a227; margin-top: 24px; border-bottom: 1px solid #eee; padding-bottom: 8px;">{category}</h3>\n'
        for link in cat_links:
            html_content += '<div style="margin-bottom: 20px;">\n'
            html_content += f'  <p style="margin: 0;"><strong><a href="{link["url"]}" style="color: #1a1a1a;">{link["title"]}</a></strong>'
            if link['source']:
                html_content += f' <span style="color: #888;">— {link["source"]}</span>'
            html_content += '</p>\n'
            if link['description']:
                html_content += f'  <p style="margin: 4px 0; color: #333; font-size: 15px;">{link["description"]}</p>\n'
            if link['notes']:
                html_content += f'  <p style="margin: 4px 0; color: #666; font-style: italic; font-size: 14px;">{link["notes"]}</p>\n'
            html_content += '</div>\n'

    html_content += '</div>'

    # Publish to Beehiiv
    result = publish_to_beehiiv(title, html_content, status='draft')

    if result.get('success'):
        # Mark links as published
        db = get_db()
        cur = db.cursor()
        link_ids = [link['id'] for link in links]
        cur.execute(
            "UPDATE links SET status = 'published', updated_at = CURRENT_TIMESTAMP WHERE id = ANY(%s)",
            (link_ids,)
        )
        db.commit()
        cur.close()

    return jsonify(result)


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
