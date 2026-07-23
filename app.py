import streamlit as st
import requests
from bs4 import BeautifulSoup
import re
import random
import time
import json
from urllib.parse import urljoin, urlparse
import pandas as pd
from io import StringIO
import cloudscraper

# ---------- SESSION STATE ----------
if 'is_ready' not in st.session_state:
    st.session_state.is_ready = False
if 'csv_data' not in st.session_state:
    st.session_state.csv_data = None
if 'df_preview' not in st.session_state:
    st.session_state.df_preview = None
if 'failed_urls' not in st.session_state:
    st.session_state.failed_urls = []

# ---------- USER-AGENTS ----------
USER_AGENTS = [
    'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36',
    'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36',
    'Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:109.0) Gecko/20100101 Firefox/126.0',
]

def get_headers():
    return {'User-Agent': random.choice(USER_AGENTS)}

# ---------- UNIVERSAL EXTRACTORS (Class-independent) ----------
def extract_phone_from_text(text):
    # US/CA style: (123) 456-7890, 123-456-7890, 123.456.7890, 1234567890
    phones = re.findall(r'\(?\d{3}\)?[-.\s]?\d{3}[-.\s]?\d{4}', text)
    # Clean duplicates
    return list(set(phones))

def extract_email_from_text(text):
    emails = re.findall(r'[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}', text)
    return list(set(emails))

def extract_website_from_soup(soup, base_url):
    # Pehle 'mailto:' nahi, bas 'http' wale links dhoondho
    for a in soup.find_all('a', href=True):
        href = a['href']
        # Agar href 'http' se start ho raha hai aur yeh same domain nahi hai (external) toh website ho sakti hai
        # Ya agar href mein 'http' nahi hai lekin '/', toh full URL banao
        if href.startswith('http') and 'mailto:' not in href and 'tel:' not in href:
            return href
        elif href.startswith('/') and len(href) > 1:
            # Internal link, but if nothing else, we can construct
            pass
    return ''

# ---------- SCRAPERS (FIXED) ----------
def scrape_yelp(url):
    scraper = cloudscraper.create_scraper()
    try:
        resp = scraper.get(url, headers=get_headers(), timeout=20)
        if resp.status_code != 200:
            return None
        
        soup = BeautifulSoup(resp.text, 'lxml')
        
        # Try 1: JSON-LD
        name = ''
        phone = ''
        address = ''
        website = ''
        rating = ''
        categories = ''
        
        for script in soup.find_all('script', type='application/ld+json'):
            try:
                data = json.loads(script.string)
                if data.get('@type') == 'LocalBusiness':
                    name = data.get('name', name)
                    phone = data.get('telephone', phone)
                    address = data.get('address', {}).get('streetAddress', address) if isinstance(data.get('address'), dict) else address
                    website = data.get('url', website)
                    rating = data.get('aggregateRating', {}).get('ratingValue', rating) if isinstance(data.get('aggregateRating'), dict) else rating
                    break
            except: pass
        
        # Try 2: Apollo state (if JSON-LD failed)
        if not name:
            for script in soup.find_all('script'):
                if script.string and '__APOLLO_STATE__' in script.string:
                    try:
                        json_str = re.search(r'window\.__APOLLO_STATE__\s*=\s*({.*?});', script.string, re.DOTALL)
                        if json_str:
                            data = json.loads(json_str.group(1))
                            for key, value in data.items():
                                if key.startswith('Business:') or key.startswith('BusinessV2:'):
                                    biz = value
                                    name = biz.get('name', name)
                                    phone = biz.get('displayPhone', phone)
                                    addr = biz.get('location', {})
                                    if isinstance(addr, dict):
                                        address = addr.get('address1', address)
                                    website = biz.get('websiteUrl', website)
                                    rating = biz.get('rating', rating)
                                    categories = ', '.join([c.get('title', '') for c in biz.get('categories', []) if isinstance(c, dict)])
                                    break
                    except: pass
        
        # Try 3: Fallback HTML scraping (last resort)
        if not name:
            h1 = soup.find('h1')
            if h1:
                name = h1.get_text(strip=True)
        
        if not phone:
            phone = extract_phone_from_text(resp.text)
            phone = phone[0] if phone else ''
        
        if not address:
            addr_div = soup.find('address')
            if addr_div:
                address = addr_div.get_text(strip=True)
            else:
                # Try to find text with address pattern
                for p in soup.find_all(['p', 'div']):
                    txt = p.get_text()
                    if re.search(r'\d{5}', txt) and re.search(r'Street|St|Ave|Blvd', txt, re.I):
                        address = txt.strip()
                        break
        
        if not website:
            website = extract_website_from_soup(soup, url)
        
        if name:
            return {
                'name': name.strip(),
                'phone': phone,
                'address': address,
                'website': website,
                'rating': rating,
                'source': 'Yelp',
                'categories': categories
            }
        return None
    except Exception as e:
        return None

def scrape_citylocal(url):
    try:
        resp = requests.get(url, headers=get_headers(), timeout=15)
        if resp.status_code != 200:
            return None
        soup = BeautifulSoup(resp.text, 'lxml')
        
        # Name
        name = soup.find('h1').get_text(strip=True) if soup.find('h1') else ''
        if not name:
            return None  # Agar name nahi mila toh waste hai
        
        # Phone - via regex
        phone_list = extract_phone_from_text(resp.text)
        phone = phone_list[0] if phone_list else ''
        
        # Email - via regex
        email_list = extract_email_from_text(resp.text)
        general_email = email_list[0] if email_list else ''
        
        # Address - Generic searching
        address = ''
        addr_div = soup.find('div', class_=re.compile(r'addr|loc|contact', re.I))
        if addr_div:
            address = addr_div.get_text(strip=True)
        else:
            # Try to find using text patterns (city, state, zip)
            for p in soup.find_all(['p', 'div', 'span']):
                txt = p.get_text()
                if re.search(r'\b\d{5}\b', txt) and re.search(r'Street|St|Ave|Blvd|Road|Rd', txt, re.I):
                    address = txt.strip()
                    break
        
        # Website - Generic
        website = extract_website_from_soup(soup, url)
        
        # If no website found via 'href', try searching text for 'http'
        if not website:
            web_match = re.search(r'(https?://[^\s"\']+)', resp.text)
            if web_match:
                website = web_match.group(1)
        
        return {
            'name': name,
            'phone': phone,
            'address': address,
            'website': website,
            'rating': '',
            'source': 'CityLocal101',
            'categories': ''
        }
    except Exception as e:
        return None

def scrape_bbb(url):
    # Same logic, using cloudscraper
    scraper = cloudscraper.create_scraper()
    try:
        resp = scraper.get(url, headers=get_headers(), timeout=15)
        if resp.status_code != 200: return None
        soup = BeautifulSoup(resp.text, 'lxml')
        name = ''
        phone = ''
        address = ''
        website = ''
        rating = ''
        # Try JSON-LD
        for script in soup.find_all('script', type='application/ld+json'):
            try:
                data = json.loads(script.string)
                if data.get('@type') in ['Organization', 'LocalBusiness']:
                    name = data.get('name', name)
                    phone = data.get('telephone', phone)
                    address = data.get('address', {}).get('streetAddress', address) if isinstance(data.get('address'), dict) else address
                    website = data.get('url', website)
                    rating = data.get('aggregateRating', {}).get('ratingValue', rating) if isinstance(data.get('aggregateRating'), dict) else rating
                    break
            except: pass
        if not name:
            h1 = soup.find('h1')
            if h1: name = h1.get_text(strip=True)
        if not website:
            web_anchor = soup.find('a', string=re.compile(r'Website', re.I))
            if web_anchor and web_anchor.get('href'): website = web_anchor['href']
        if name:
            return {'name': name, 'phone': phone, 'address': address, 'website': website, 'rating': rating, 'source': 'BBB', 'categories': ''}
        return None
    except: return None

def scrape_yellowpages(url):
    scraper = cloudscraper.create_scraper()
    try:
        resp = scraper.get(url, headers=get_headers(), timeout=15)
        if resp.status_code != 200: return None
        soup = BeautifulSoup(resp.text, 'lxml')
        name = soup.find('h1', {'class': re.compile(r'business-name', re.I)})
        name = name.get_text(strip=True) if name else ''
        if not name: return None
        
        phone = extract_phone_from_text(resp.text)
        phone = phone[0] if phone else ''
        address = ''
        addr_div = soup.find('div', {'class': re.compile(r'address', re.I)})
        if addr_div: address = addr_div.get_text(strip=True)
        website = extract_website_from_soup(soup, url)
        
        return {'name': name, 'phone': phone, 'address': address, 'website': website, 'rating': '', 'source': 'YellowPages', 'categories': ''}
    except: return None

def search_google_places(query, api_key):
    if not api_key: return []
    url = "https://maps.googleapis.com/maps/api/place/textsearch/json"
    params = {'query': query, 'key': api_key, 'fields': 'name,formatted_address,formatted_phone_number,website,rating,place_id,types'}
    try:
        resp = requests.get(url, params=params, timeout=15)
        if resp.status_code != 200: return []
        data = resp.json()
        if data.get('status') != 'OK': return []
        results = []
        for item in data.get('results', []):
            place_id = item.get('place_id')
            phone = item.get('formatted_phone_number', '')
            if not phone and place_id:
                details_url = "https://maps.googleapis.com/maps/api/place/details/json"
                details_params = {'place_id': place_id, 'key': api_key, 'fields': 'formatted_phone_number,website,url'}
                try:
                    details_resp = requests.get(details_url, params=details_params, timeout=10)
                    if details_resp.status_code == 200:
                        details_data = details_resp.json()
                        if details_data.get('status') == 'OK':
                            result = details_data.get('result', {})
                            phone = result.get('formatted_phone_number', phone)
                            website = result.get('website', '')
                except: pass
            results.append({
                'name': item.get('name', ''),
                'phone': phone,
                'address': item.get('formatted_address', ''),
                'website': item.get('website', ''),
                'rating': item.get('rating', ''),
                'source': 'Google Places',
                'categories': ', '.join(item.get('types', []))
            })
        return results
    except: return []

# ---------- ENRICHMENT AND DECISION MAKER FINDER (Same as before but improved) ----------
def find_decision_maker(soup, domain):
    priority_keywords = ['owner', 'ceo', 'founder', 'president', 'director', 'head of', 'manager', 'proprietor']
    decision_makers = []
    text = soup.get_text()
    for title in priority_keywords:
        pattern = re.compile(rf'([A-Z][a-z]+(?:\s[A-Z][a-z]+)*)\s*[-–—]\s*{title}', re.IGNORECASE)
        matches = pattern.findall(text)
        for name in matches:
            if len(name) > 2:
                decision_makers.append({'name': name.strip(), 'title': title.capitalize(), 'phone': ''})
    for elem in soup.find_all(['h1', 'h2', 'h3', 'strong', 'b', 'p']):
        elem_text = elem.get_text(strip=True)
        for title in priority_keywords:
            if title in elem_text.lower():
                name_match = re.search(r'([A-Z][a-z]+(?:\s[A-Z][a-z]+)*)', elem_text)
                if name_match:
                    name = name_match.group(1)
                    phone = ''
                    phone_match = re.search(r'\(?\d{3}\)?[-.\s]?\d{3}[-.\s]?\d{4}', elem_text)
                    if phone_match: phone = phone_match.group()
                    else:
                        nxt = elem.find_next_sibling()
                        if nxt and nxt.name == 'p':
                            nxt_text = nxt.get_text()
                            phone_match = re.search(r'\(?\d{3}\)?[-.\s]?\d{3}[-.\s]?\d{4}', nxt_text)
                            if phone_match: phone = phone_match.group()
                    decision_makers.append({'name': name.strip(), 'title': title.capitalize(), 'phone': phone})
    unique = []
    seen = set()
    for dm in decision_makers:
        if dm['name'] not in seen:
            seen.add(dm['name'])
            unique.append(dm)
    return unique

def get_primary_decision_maker(dms):
    if not dms: return None
    priority_order = ['owner', 'ceo', 'founder', 'president', 'director', 'head', 'manager']
    def get_priority(dm):
        title_lower = dm.get('title', '').lower()
        for i, p in enumerate(priority_order):
            if p in title_lower:
                return i
        return 999
    sorted_dms = sorted(dms, key=get_priority)
    return sorted_dms[0]

def enrich_website(website_url):
    if not website_url or website_url == '':
        return {'emails': [], 'social': {}, 'decision_makers': [], 'gap_analysis': {'website_alive': False}}
    try:
        if not website_url.startswith('http'):
            website_url = 'https://' + website_url
        try:
            head_resp = requests.head(website_url, timeout=5, headers=get_headers())
            if head_resp.status_code >= 400:
                return {'emails': [], 'social': {}, 'decision_makers': [], 'gap_analysis': {'website_alive': False}}
        except:
            return {'emails': [], 'social': {}, 'decision_makers': [], 'gap_analysis': {'website_alive': False}}
        resp = requests.get(website_url, timeout=10, headers=get_headers())
        if resp.status_code != 200:
            return {'emails': [], 'social': {}, 'decision_makers': [], 'gap_analysis': {'website_alive': False}}
        soup = BeautifulSoup(resp.text, 'lxml')
        emails = extract_email_from_text(resp.text)
        social = {'facebook': '', 'instagram': '', 'linkedin': '', 'twitter': ''}
        for a in soup.find_all('a', href=True):
            href = a['href']
            if 'facebook.com' in href: social['facebook'] = href
            elif 'instagram.com' in href: social['instagram'] = href
            elif 'linkedin.com/company' in href or 'linkedin.com/in' in href: social['linkedin'] = href
            elif 'twitter.com' in href or 'x.com' in href: social['twitter'] = href
        decision_makers = find_decision_maker(soup, website_url)
        for path in ['/about', '/about-us', '/team', '/leadership', '/our-team']:
            try:
                about_url = urljoin(website_url, path)
                about_resp = requests.get(about_url, timeout=8, headers=get_headers())
                if about_resp.status_code == 200:
                    about_soup = BeautifulSoup(about_resp.text, 'lxml')
                    decision_makers.extend(find_decision_maker(about_soup, website_url))
                    emails.extend(extract_email_from_text(about_resp.text))
                    for a in about_soup.find_all('a', href=True):
                        href = a['href']
                        if 'facebook.com' in href: social['facebook'] = href
                        elif 'instagram.com' in href: social['instagram'] = href
                        elif 'linkedin.com/company' in href or 'linkedin.com/in' in href: social['linkedin'] = href
            except: pass
        emails = list(set(emails))
        primary_dm_email = ''
        if decision_makers:
            primary = get_primary_decision_maker(decision_makers)
            if primary:
                first_name = primary['name'].split()[0].lower()
                last_name = primary['name'].split()[-1].lower()
                domain = urlparse(website_url).netloc.replace('www.', '')
                patterns = [
                    f"{first_name}.{last_name}@{domain}",
                    f"{first_name}{last_name}@{domain}",
                    f"{first_name[0]}{last_name}@{domain}",
                    f"{first_name}@{domain}"
                ]
                for pattern in patterns:
                    if pattern in resp.text or pattern in str(emails):
                        primary_dm_email = pattern
                        break
                if not primary_dm_email:
                    for email in emails:
                        if domain in email:
                            primary_dm_email = email
                            break
        gap = {
            'website_alive': True,
            'has_social': bool(social['facebook'] or social['instagram'] or social['linkedin']),
            'has_email': bool(emails),
            'has_decision_maker': bool(decision_makers)
        }
        return {
            'emails': emails,
            'social': social,
            'decision_makers': decision_makers,
            'primary_dm_email': primary_dm_email,
            'gap_analysis': gap
        }
    except Exception as e:
        return {'emails': [], 'social': {}, 'decision_makers': [], 'gap_analysis': {'website_alive': False}}

def generate_pitch(gap):
    if not gap.get('website_alive'): return "🚨 No website → Pitch: Website Development (WordPress/Shopify) + Digital Success Blueprint"
    if not gap.get('has_social'): return "📱 No social media → Pitch: Social Media Management + SMM Ads + Content Writing"
    if not gap.get('has_email'): return "📧 No email found → Pitch: Email Marketing Setup + Lead Generation Funnels"
    if not gap.get('has_decision_maker'): return "👤 Decision maker not found → Pitch: LinkedIn Outreach + Direct Sales Strategy"
    return "🏆 Complete presence → Pitch: AI Chatbot Integration + Power BI Analytics + CRO (Upsell)"

def process_leads(inputs, mode, api_key):
    all_rows = []
    failed_items = []
    if mode == 'urls':
        urls = [u.strip() for u in inputs.split('\n') if u.strip().startswith('http')]
        for url in urls:
            result = None
            if 'bbb.org' in url: result = scrape_bbb(url)
            elif 'yelp.com' in url: result = scrape_yelp(url)
            elif 'yellowpages.com' in url: result = scrape_yellowpages(url)
            elif 'citylocal101.com' in url: result = scrape_citylocal(url)
            elif 'linkedin.com' in url:
                name = url.split('/company/')[-1].replace('-', ' ').title() if '/company/' in url else url.split('/')[-1].replace('-', ' ').title()
                result = {'name': name, 'phone': '', 'address': '', 'website': '', 'rating': '', 'source': 'LinkedIn', 'categories': ''}
            else:
                result = None
            if result and result.get('name'):
                enriched = enrich_website(result.get('website', ''))
                dms = enriched.get('decision_makers', [])
                primary_dm = get_primary_decision_maker(dms) if dms else None
                if primary_dm:
                    dm_name = primary_dm.get('name', '')
                    dm_title = primary_dm.get('title', '')
                    dm_phone = primary_dm.get('phone', '')
                else:
                    dm_name = dm_title = dm_phone = ''
                row = {
                    'Business Name': result.get('name', ''),
                    'Phone': result.get('phone', ''),
                    'Address': result.get('address', ''),
                    'Website': result.get('website', ''),
                    'Rating': result.get('rating', ''),
                    'Categories': result.get('categories', ''),
                    'Source': result.get('source', ''),
                    'General Emails': ', '.join(enriched.get('emails', [])),
                    'Facebook': enriched.get('social', {}).get('facebook', ''),
                    'Instagram': enriched.get('social', {}).get('instagram', ''),
                    'LinkedIn Page': enriched.get('social', {}).get('linkedin', ''),
                    'Owner/Decision Maker Name': dm_name,
                    'Owner/Decision Maker Title': dm_title,
                    'Owner/Decision Maker Phone': dm_phone,
                    'Owner/Decision Maker Direct Email': enriched.get('primary_dm_email', ''),
                    'Gap Analysis': f"Website Alive: {enriched.get('gap_analysis', {}).get('website_alive', False)}, Social: {enriched.get('gap_analysis', {}).get('has_social', False)}, Email: {enriched.get('gap_analysis', {}).get('has_email', False)}, Decision Maker: {enriched.get('gap_analysis', {}).get('has_decision_maker', False)}",
                    'Pitch Direction': generate_pitch(enriched.get('gap_analysis', {}))
                }
                all_rows.append(row)
            else:
                failed_items.append(url)
    else:
        if not api_key:
            st.error("⚠️ API Key required.")
            return [], []
        businesses = search_google_places(inputs, api_key)
        for biz in businesses:
            if biz.get('name'):
                enriched = enrich_website(biz.get('website', ''))
                dms = enriched.get('decision_makers', [])
                primary_dm = get_primary_decision_maker(dms) if dms else None
                if primary_dm:
                    dm_name = primary_dm.get('name', '')
                    dm_title = primary_dm.get('title', '')
                    dm_phone = primary_dm.get('phone', '')
                else:
                    dm_name = dm_title = dm_phone = ''
                row = {
                    'Business Name': biz.get('name', ''),
                    'Phone': biz.get('phone', ''),
                    'Address': biz.get('address', ''),
                    'Website': biz.get('website', ''),
                    'Rating': biz.get('rating', ''),
                    'Categories': biz.get('categories', ''),
                    'Source': biz.get('source', 'Google Places'),
                    'General Emails': ', '.join(enriched.get('emails', [])),
                    'Facebook': enriched.get('social', {}).get('facebook', ''),
                    'Instagram': enriched.get('social', {}).get('instagram', ''),
                    'LinkedIn Page': enriched.get('social', {}).get('linkedin', ''),
                    'Owner/Decision Maker Name': dm_name,
                    'Owner/Decision Maker Title': dm_title,
                    'Owner/Decision Maker Phone': dm_phone,
                    'Owner/Decision Maker Direct Email': enriched.get('primary_dm_email', ''),
                    'Gap Analysis': f"Website Alive: {enriched.get('gap_analysis', {}).get('website_alive', False)}, Social: {enriched.get('gap_analysis', {}).get('has_social', False)}, Email: {enriched.get('gap_analysis', {}).get('has_email', False)}, Decision Maker: {enriched.get('gap_analysis', {}).get('has_decision_maker', False)}",
                    'Pitch Direction': generate_pitch(enriched.get('gap_analysis', {}))
                }
                all_rows.append(row)
    return all_rows, failed_items

# ---------- STREAMLIT UI ----------
st.set_page_config(page_title="Sales Intelligence Engine", page_icon="🦊")
st.title("🦊 Sales Intelligence Engine V2.1 (FIXED)")
st.markdown("**Ab Yelp aur CityLocal101 dono se data properly nikalega**")

with st.expander("📌 Fixes Applied", expanded=True):
    st.write("""
    - **Yelp:** Ab JSON-LD aur Apollo State dono parse karta hai. Phone/Address/Website regex se nikaalta hai.
    - **CityLocal101:** Class-independent extraction. Phone, Email, Website ab regex aur link analysis se nikalte hain.
    """)

mode = st.radio("Select Input Mode:", ["Paste URLs", "Keyword Search (Service + City)"])

api_key = ""
if mode == "Keyword Search (Service + City)":
    api_key = st.text_input("🔑 Google Places API Key:", type="password")

if mode == "Paste URLs":
    urls_input = st.text_area("🔗 Paste URLs (One per line):", height=200)
else:
    urls_input = st.text_input("🔍 Enter Service + City:", placeholder="Plumbing Chicago")

if st.button("🚀 Generate Leads", type="primary"):
    if not urls_input.strip():
        st.error("❌ Kuch toh daalo!")
    else:
        with st.spinner("Processing..."):
            rows, failed = process_leads(urls_input.strip(), 'urls' if mode == "Paste URLs" else 'keyword', api_key)
            if rows:
                df = pd.DataFrame(rows)
                csv_buffer = StringIO()
                df.to_csv(csv_buffer, index=False, encoding='utf-8-sig')
                csv_data = csv_buffer.getvalue()
                st.session_state.csv_data = csv_data
                st.session_state.df_preview = df
                st.session_state.failed_urls = failed
                st.session_state.is_ready = True
                st.rerun()
            else:
                st.error("❌ Koi lead nahi mili. Check URLs/Keyword or API Key.")

if st.session_state.is_ready:
    st.success(f"✅ {len(st.session_state.df_preview)} leads generated! {len(st.session_state.failed_urls)} failed.")
    if st.session_state.failed_urls:
        with st.expander(f"⚠️ Failed URLs ({len(st.session_state.failed_urls)})"):
            st.write('\n'.join(st.session_state.failed_urls))
    st.subheader("📊 Preview (First 10 rows)")
    st.dataframe(st.session_state.df_preview.head(10))
    col1, col2 = st.columns([3, 1])
    with col1:
        st.download_button(label="⬇️ Download CSV", data=st.session_state.csv_data, file_name=f"sales_leads_{int(time.time())}.csv", mime="text/csv", use_container_width=True)
    with col2:
        if st.button("🔄 Reset", use_container_width=True):
            st.session_state.is_ready = False
            st.rerun()

st.caption("🦊 V2.1: Robust Phone/Email/Website extraction using RegEx & smart link analysis")
