"""
Token Trader Admin Portal

Unified admin for:
- Article scraping and curation
- Benchmark testing (Inference Price Index)
- Newsletter composition

Vercel + Postgres version.
"""

import os
import re
import json
import requests
import subprocess
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

# Categories aligned with Token Trader value chain
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
    'Power & Energy',
    'Other',
]

STATUSES = ['scraped', 'saved', 'scheduled', 'published', 'archived']

# Relevance scoring keywords by tier
RELEVANCE_KEYWORDS = {
    # Tier 1 - Core value chain (10 points each)
    'tier1': [
        'inference pricing', 'inference cost', 'cost per token', 'price per token',
        'compute futures', 'gpu rental', 'gpu leasing', 'ocpi',
        'nvidia earnings', 'nvidia revenue', 'h100', 'h200', 'b100', 'b200',
        'coreweave', 'lambda labs', 'together ai',
    ],
    # Tier 2 - Important context (5 points each)
    'tier2': [
        'openai pricing', 'anthropic pricing', 'google ai pricing', 'claude pricing',
        'gpt-4 pricing', 'gemini pricing', 'api pricing',
        'data center power', 'pjm', 'ercot', 'power purchase',
        'cftc', 'commodity futures', 'derivatives',
        'inference api', 'model serving', 'mlops',
    ],
    # Tier 3 - Related topics (2 points each)
    'tier3': [
        'nvidia', 'amd', 'intel', 'tpu', 'gpu',
        'aws', 'azure', 'gcp', 'cloud compute',
        'llm', 'large language model', 'foundation model',
        'openai', 'anthropic', 'google ai', 'meta ai',
        'enterprise ai', 'ai adoption',
    ],
}

# RSS feed sources
RSS_FEEDS = [
    {'name': 'TechCrunch AI', 'url': 'https://techcrunch.com/category/artificial-intelligence/feed/', 'category': 'Enterprise AI'},
    {'name': 'Ars Technica AI', 'url': 'https://feeds.arstechnica.com/arstechnica/technology-lab', 'category': 'Model Economics'},
    {'name': 'The Verge AI', 'url': 'https://www.theverge.com/rss/ai-artificial-intelligence/index.xml', 'category': 'Enterprise AI'},
    {'name': 'Hacker News', 'url': 'https://hnrss.org/newest?q=llm+OR+gpu+OR+inference', 'category': 'Other'},
]

# Google News search queries for value chain
GOOGLE_NEWS_QUERIES = [
    {'query': 'NVIDIA earnings GPU revenue', 'category': 'GPU Markets'},
    {'query': 'H100 H200 pricing availability', 'category': 'GPU Markets'},
    {'query': 'CoreWeave Lambda Labs GPU cloud', 'category': 'Cloud Providers'},
    {'query': 'OpenAI API pricing tokens', 'category': 'Inference Pricing'},
    {'query': 'Anthropic Claude pricing API', 'category': 'Inference Pricing'},
    {'query': 'data center power PJM electricity', 'category': 'Power & Energy'},
    {'query': 'AI compute futures derivatives', 'category': 'Compute Futures'},
    {'query': 'enterprise AI adoption spending', 'category': 'Enterprise AI'},
    {'query': 'LLM inference cost optimization', 'category': 'Model Economics'},
]


def get_db():
    """Get database connection."""
    if 'db' not in g:
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

    # Links table with scraping fields
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
            relevance_score INTEGER DEFAULT 0,
            scraped_from TEXT,
            published_date TIMESTAMP,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    ''')

    # Benchmark runs table
    cur.execute('''
        CREATE TABLE IF NOT EXISTS benchmark_runs (
            id SERIAL PRIMARY KEY,
            run_id TEXT NOT NULL UNIQUE,
            run_date TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            status TEXT DEFAULT 'pending',
            models_tested INTEGER DEFAULT 0,
            total_calls INTEGER DEFAULT 0,
            total_cost DECIMAL(10, 4) DEFAULT 0,
            results_json TEXT,
            newsletter_md TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
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
            last_scraped TIMESTAMP
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


def calculate_relevance(title, description):
    """Calculate relevance score based on keyword tiers."""
    text = f"{title} {description}".lower()
    score = 0

    for keyword in RELEVANCE_KEYWORDS['tier1']:
        if keyword.lower() in text:
            score += 10
    for keyword in RELEVANCE_KEYWORDS['tier2']:
        if keyword.lower() in text:
            score += 5
    for keyword in RELEVANCE_KEYWORDS['tier3']:
        if keyword.lower() in text:
            score += 2

    return min(score, 100)  # Cap at 100


def fetch_metadata(url):
    """Fetch Open Graph and meta data from URL."""
    try:
        headers = {
            'User-Agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36'
        }
        response = requests.get(url, headers=headers, timeout=10)
        html = response.text

        og_title = re.search(r'<meta[^>]*property=["\']og:title["\'][^>]*content=["\']([^"\']+)["\']', html, re.I)
        og_desc = re.search(r'<meta[^>]*property=["\']og:description["\'][^>]*content=["\']([^"\']+)["\']', html, re.I)
        og_image = re.search(r'<meta[^>]*property=["\']og:image["\'][^>]*content=["\']([^"\']+)["\']', html, re.I)
        og_site = re.search(r'<meta[^>]*property=["\']og:site_name["\'][^>]*content=["\']([^"\']+)["\']', html, re.I)

        if not og_title:
            og_title = re.search(r'<title[^>]*>([^<]+)</title>', html, re.I)
        if not og_desc:
            og_desc = re.search(r'<meta[^>]*name=["\']description["\'][^>]*content=["\']([^"\']+)["\']', html, re.I)

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
        parsed = urlparse(url)
        return {
            'title': '',
            'description': '',
            'image_url': '',
            'source': parsed.netloc,
            'author': '',
        }


def parse_rss_feed(feed_url, source_name, category):
    """Parse RSS feed and return articles."""
    articles = []
    try:
        response = requests.get(feed_url, timeout=15, headers={
            'User-Agent': 'Mozilla/5.0 (compatible; TokenTrader/1.0)'
        })
        root = ET.fromstring(response.content)

        # Handle different RSS formats
        items = root.findall('.//item') or root.findall('.//{http://www.w3.org/2005/Atom}entry')

        cutoff = datetime.now() - timedelta(days=4)

        for item in items[:20]:  # Limit to 20 per feed
            title = item.findtext('title') or item.findtext('{http://www.w3.org/2005/Atom}title') or ''
            link = item.findtext('link') or ''
            if not link:
                link_elem = item.find('{http://www.w3.org/2005/Atom}link')
                if link_elem is not None:
                    link = link_elem.get('href', '')

            description = item.findtext('description') or item.findtext('{http://www.w3.org/2005/Atom}summary') or ''
            description = re.sub(r'<[^>]+>', '', description)[:500]  # Strip HTML, limit length

            pub_date = item.findtext('pubDate') or item.findtext('{http://www.w3.org/2005/Atom}published')

            if title and link:
                relevance = calculate_relevance(title, description)
                articles.append({
                    'url': link,
                    'title': unescape(title),
                    'description': unescape(description),
                    'source': source_name,
                    'category': category,
                    'relevance_score': relevance,
                    'scraped_from': f'rss:{source_name}',
                })
    except Exception as e:
        print(f"Error parsing RSS {feed_url}: {e}")

    return articles


def search_google_news(query, category):
    """Search Google News RSS for articles."""
    articles = []
    try:
        encoded_query = quote_plus(query)
        # Google News RSS with date filter
        url = f"https://news.google.com/rss/search?q={encoded_query}+when:4d&hl=en-US&gl=US&ceid=US:en"

        response = requests.get(url, timeout=15, headers={
            'User-Agent': 'Mozilla/5.0 (compatible; TokenTrader/1.0)'
        })
        root = ET.fromstring(response.content)

        for item in root.findall('.//item')[:10]:  # Limit to 10 per query
            title = item.findtext('title') or ''
            link = item.findtext('link') or ''
            description = item.findtext('description') or ''
            description = re.sub(r'<[^>]+>', '', description)[:500]

            # Extract source from title (Google News format: "Title - Source")
            source = 'Google News'
            if ' - ' in title:
                parts = title.rsplit(' - ', 1)
                if len(parts) == 2:
                    title, source = parts

            if title and link:
                relevance = calculate_relevance(title, description)
                articles.append({
                    'url': link,
                    'title': unescape(title),
                    'description': unescape(description),
                    'source': source,
                    'category': category,
                    'relevance_score': relevance,
                    'scraped_from': f'google_news:{query}',
                })
    except Exception as e:
        print(f"Error searching Google News for '{query}': {e}")

    return articles


# ============================================================
# ROUTES - Authentication
# ============================================================

@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        if request.form.get('password') == PASSWORD:
            session['authenticated'] = True
            return redirect(url_for('dashboard'))
        return render_template('login.html', error='Invalid password')
    return render_template('login.html')


@app.route('/logout')
def logout():
    session.pop('authenticated', None)
    return redirect(url_for('login'))


# ============================================================
# ROUTES - Dashboard
# ============================================================

@app.route('/')
@require_auth
def dashboard():
    """Main admin dashboard."""
    db = get_db()
    cur = db.cursor()

    # Get link counts by status
    link_counts = {}
    for status in STATUSES:
        cur.execute('SELECT COUNT(*) as count FROM links WHERE status = %s', (status,))
        link_counts[status] = cur.fetchone()['count']

    # Get recent benchmark runs
    cur.execute('''
        SELECT * FROM benchmark_runs
        ORDER BY created_at DESC LIMIT 5
    ''')
    recent_benchmarks = cur.fetchall()

    # Get high-relevance articles
    cur.execute('''
        SELECT * FROM links
        WHERE status = 'scraped' AND relevance_score >= 15
        ORDER BY relevance_score DESC, created_at DESC
        LIMIT 10
    ''')
    high_relevance = cur.fetchall()

    cur.close()

    return render_template('dashboard.html',
                          link_counts=link_counts,
                          recent_benchmarks=recent_benchmarks,
                          high_relevance=high_relevance)


# ============================================================
# ROUTES - Link Management
# ============================================================

@app.route('/links')
@require_auth
def links():
    """Link management page."""
    db = get_db()
    cur = db.cursor()

    status_filter = request.args.get('status', '')
    category_filter = request.args.get('category', '')
    issue_filter = request.args.get('issue', '')
    min_relevance = request.args.get('min_relevance', '')

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
    if min_relevance:
        query += ' AND relevance_score >= %s'
        params.append(int(min_relevance))

    query += ' ORDER BY relevance_score DESC, priority DESC, created_at DESC'

    cur.execute(query, params)
    links = cur.fetchall()

    cur.execute("SELECT DISTINCT issue FROM links WHERE issue != '' ORDER BY issue DESC")
    issues = cur.fetchall()

    counts = {}
    for status in STATUSES:
        cur.execute('SELECT COUNT(*) as count FROM links WHERE status = %s', (status,))
        counts[status] = cur.fetchone()['count']

    cur.close()

    return render_template('links.html',
                          links=links,
                          categories=CATEGORIES,
                          statuses=STATUSES,
                          issues=[i['issue'] for i in issues],
                          counts=counts,
                          filters={
                              'status': status_filter,
                              'category': category_filter,
                              'issue': issue_filter,
                              'min_relevance': min_relevance,
                          })


@app.route('/links/add', methods=['GET', 'POST'])
@require_auth
def add_link():
    if request.method == 'POST':
        url = request.form.get('url', '').strip()

        if not url:
            return render_template('add_link.html', categories=CATEGORIES, error='URL is required')

        db = get_db()
        cur = db.cursor()
        cur.execute('SELECT id FROM links WHERE url = %s', (url,))
        existing = cur.fetchone()
        if existing:
            cur.close()
            return redirect(url_for('edit_link', link_id=existing['id']))

        metadata = fetch_metadata(url)

        title = request.form.get('title', '').strip() or metadata['title']
        description = request.form.get('description', '').strip() or metadata['description']
        source = request.form.get('source', '').strip() or metadata['source']
        author = request.form.get('author', '').strip() or metadata['author']
        category = request.form.get('category', 'Other')
        tags = request.form.get('tags', '').strip()
        notes = request.form.get('notes', '').strip()
        issue = request.form.get('issue', '').strip()
        priority = int(request.form.get('priority', 0))
        relevance = calculate_relevance(title, description)

        cur.execute('''
            INSERT INTO links (url, title, description, source, author, image_url, category, tags, notes, issue, priority, relevance_score, status)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, 'saved')
        ''', (url, title, description, source, author, metadata['image_url'], category, tags, notes, issue, priority, relevance))
        db.commit()
        cur.close()

        return redirect(url_for('links'))

    return render_template('add_link.html', categories=CATEGORIES)


@app.route('/links/edit/<int:link_id>', methods=['GET', 'POST'])
@require_auth
def edit_link(link_id):
    db = get_db()
    cur = db.cursor()
    cur.execute('SELECT * FROM links WHERE id = %s', (link_id,))
    link = cur.fetchone()

    if not link:
        cur.close()
        return redirect(url_for('links'))

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

        return redirect(url_for('links'))

    cur.close()
    return render_template('edit_link.html', link=link, categories=CATEGORIES, statuses=STATUSES)


@app.route('/links/delete/<int:link_id>', methods=['POST'])
@require_auth
def delete_link(link_id):
    db = get_db()
    cur = db.cursor()
    cur.execute('DELETE FROM links WHERE id = %s', (link_id,))
    db.commit()
    cur.close()
    return redirect(url_for('links'))


@app.route('/links/bulk-update', methods=['POST'])
@require_auth
def bulk_update():
    link_ids = request.form.getlist('link_ids')
    action = request.form.get('action')

    if not link_ids:
        return redirect(url_for('links'))

    db = get_db()
    cur = db.cursor()
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
    return redirect(url_for('links'))


# ============================================================
# ROUTES - Scraping
# ============================================================

@app.route('/scrape')
@require_auth
def scrape_page():
    """Scraping control page."""
    return render_template('scrape.html',
                          rss_feeds=RSS_FEEDS,
                          google_queries=GOOGLE_NEWS_QUERIES)


@app.route('/scrape/run', methods=['POST'])
@require_auth
def run_scrape():
    """Run the scraper."""
    source_type = request.form.get('source_type', 'all')

    db = get_db()
    cur = db.cursor()

    all_articles = []

    # Scrape RSS feeds
    if source_type in ['all', 'rss']:
        for feed in RSS_FEEDS:
            articles = parse_rss_feed(feed['url'], feed['name'], feed['category'])
            all_articles.extend(articles)

    # Scrape Google News
    if source_type in ['all', 'google']:
        for query_config in GOOGLE_NEWS_QUERIES:
            articles = search_google_news(query_config['query'], query_config['category'])
            all_articles.extend(articles)

    # Insert into database (skip duplicates)
    new_count = 0
    for article in all_articles:
        try:
            cur.execute('SELECT id FROM links WHERE url = %s', (article['url'],))
            if not cur.fetchone():
                cur.execute('''
                    INSERT INTO links (url, title, description, source, category, relevance_score, scraped_from, status)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, 'scraped')
                ''', (
                    article['url'],
                    article['title'],
                    article['description'],
                    article['source'],
                    article['category'],
                    article['relevance_score'],
                    article['scraped_from'],
                ))
                new_count += 1
        except Exception as e:
            print(f"Error inserting article: {e}")
            db.rollback()

    db.commit()
    cur.close()

    # Store results in session for display
    session['scrape_result'] = {
        'total_found': len(all_articles),
        'new_added': new_count,
        'source_type': source_type
    }

    # Redirect to show ALL scraped articles (no min_relevance filter)
    return redirect(url_for('links', status='scraped'))


# ============================================================
# ROUTES - Benchmark
# ============================================================

@app.route('/benchmark')
@require_auth
def benchmark_page():
    """Benchmark control page."""
    db = get_db()
    cur = db.cursor()

    cur.execute('SELECT * FROM benchmark_runs ORDER BY created_at DESC LIMIT 10')
    runs = cur.fetchall()
    cur.close()

    return render_template('benchmark.html', runs=runs)


@app.route('/benchmark/results/<run_id>')
@require_auth
def benchmark_results(run_id):
    """View benchmark results."""
    db = get_db()
    cur = db.cursor()

    cur.execute('SELECT * FROM benchmark_runs WHERE run_id = %s', (run_id,))
    run = cur.fetchone()
    cur.close()

    if not run:
        return redirect(url_for('benchmark_page'))

    results = json.loads(run['results_json']) if run['results_json'] else {}

    return render_template('benchmark_results.html', run=run, results=results)


# ============================================================
# ROUTES - Newsletter Composer
# ============================================================

@app.route('/compose')
@require_auth
def compose_newsletter():
    """Newsletter composition page."""
    db = get_db()
    cur = db.cursor()

    issue = request.args.get('issue', '')

    # Get scheduled links
    query = "SELECT * FROM links WHERE status = 'scheduled'"
    params = []
    if issue:
        query += ' AND issue = %s'
        params.append(issue)
    query += ' ORDER BY category, priority DESC, relevance_score DESC'

    cur.execute(query, params)
    links = cur.fetchall()

    # Get latest benchmark
    cur.execute('''
        SELECT * FROM benchmark_runs
        WHERE status = 'completed'
        ORDER BY created_at DESC LIMIT 1
    ''')
    latest_benchmark = cur.fetchone()

    # Get available issues
    cur.execute("SELECT DISTINCT issue FROM links WHERE issue != '' ORDER BY issue DESC")
    issues = cur.fetchall()

    cur.close()

    # Group links by category
    links_by_category = {}
    for link in links:
        cat = link['category']
        if cat not in links_by_category:
            links_by_category[cat] = []
        links_by_category[cat].append(link)

    return render_template('compose.html',
                          links=links,
                          links_by_category=links_by_category,
                          latest_benchmark=latest_benchmark,
                          issues=[i['issue'] for i in issues],
                          current_issue=issue)


@app.route('/compose/preview', methods=['POST'])
@require_auth
def preview_newsletter():
    """Generate newsletter preview."""
    issue = request.form.get('issue', '')
    include_benchmark = request.form.get('include_benchmark') == 'on'
    benchmark_run_id = request.form.get('benchmark_run_id', '')
    intro_text = request.form.get('intro_text', '')

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

    # Get benchmark if requested
    benchmark_html = ''
    if include_benchmark and benchmark_run_id:
        cur.execute('SELECT * FROM benchmark_runs WHERE run_id = %s', (benchmark_run_id,))
        benchmark = cur.fetchone()
        if benchmark and benchmark['newsletter_md']:
            # Convert markdown to basic HTML (simplified)
            benchmark_html = benchmark['newsletter_md']

    cur.close()

    # Generate HTML
    html = generate_newsletter_html(links, benchmark_html, intro_text, issue)

    return render_template('preview.html', html=html, issue=issue)


def generate_newsletter_html(links, benchmark_section, intro_text, issue):
    """Generate newsletter HTML."""
    # Group by category
    by_category = {}
    for link in links:
        cat = link['category']
        if cat not in by_category:
            by_category[cat] = []
        by_category[cat].append(link)

    html = f'''
<div style="font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif; max-width: 600px; margin: 0 auto;">
    <div style="text-align: center; padding: 20px 0; border-bottom: 3px solid #c9a227;">
        <h1 style="color: #1a1a1a; margin: 0; font-size: 28px;">THE TOKEN TRADER</h1>
        <p style="color: #666; margin: 8px 0 0; font-style: italic;">Inference is the new oil.</p>
    </div>
'''

    if intro_text:
        html += f'''
    <div style="padding: 20px 0; border-bottom: 1px solid #eee;">
        <p style="color: #333; line-height: 1.6;">{intro_text}</p>
    </div>
'''

    if benchmark_section:
        html += f'''
    <div style="padding: 20px 0; border-bottom: 1px solid #eee;">
        <h2 style="color: #c9a227; font-size: 20px; margin: 0 0 16px;">This Week's Inference Price Index</h2>
        <div style="background: #f9f9f9; padding: 16px; border-radius: 8px;">
            {benchmark_section}
        </div>
    </div>
'''

    if by_category:
        html += '''
    <div style="padding: 20px 0;">
        <h2 style="color: #c9a227; font-size: 20px; margin: 0 0 16px;">What We're Reading</h2>
'''
        for category, cat_links in by_category.items():
            html += f'''
        <h3 style="color: #1a1a1a; font-size: 16px; margin: 20px 0 12px; padding-bottom: 8px; border-bottom: 1px solid #eee;">{category}</h3>
'''
            for link in cat_links:
                html += f'''
        <div style="margin-bottom: 16px;">
            <a href="{link['url']}" style="color: #1a1a1a; font-weight: 600; text-decoration: none;">{link['title']}</a>
            <span style="color: #666;"> — {link['source']}</span>
'''
                if link['description']:
                    html += f'''
            <p style="color: #444; margin: 4px 0 0; font-size: 14px; line-height: 1.5;">{link['description'][:200]}{'...' if len(link['description'] or '') > 200 else ''}</p>
'''
                if link['notes']:
                    html += f'''
            <p style="color: #666; margin: 4px 0 0; font-size: 14px; font-style: italic;">{link['notes']}</p>
'''
                html += '''
        </div>
'''
        html += '''
    </div>
'''

    html += '''
    <div style="text-align: center; padding: 20px 0; border-top: 1px solid #eee; color: #666; font-size: 12px;">
        <p>The Token Trader — The price-and-fundamentals authority for AI as a commodity.</p>
    </div>
</div>
'''

    return html


# ============================================================
# API Endpoints
# ============================================================

@app.route('/api/fetch-metadata', methods=['POST'])
@require_auth
def fetch_metadata_api():
    url = request.json.get('url', '')
    if not url:
        return jsonify({'error': 'URL required'}), 400
    metadata = fetch_metadata(url)
    metadata['relevance_score'] = calculate_relevance(metadata.get('title', ''), metadata.get('description', ''))
    return jsonify(metadata)


# ============================================================
# Initialization
# ============================================================

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
