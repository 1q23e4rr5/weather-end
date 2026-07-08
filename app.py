# ======================== app.py (نسخه نهایی با QR Code و Open-Meteo) ========================

import os
import json
import re
from datetime import datetime, timedelta
from flask import Flask, render_template, request, redirect, url_for, flash, session, jsonify
from flask_sqlalchemy import SQLAlchemy
from flask_login import LoginManager, UserMixin, login_user, logout_user, login_required, current_user
from werkzeug.security import generate_password_hash, check_password_hash
from werkzeug.utils import secure_filename
import requests
import PyPDF2
import traceback
from functools import wraps
import pandas as pd
import io
import math
import xml.etree.ElementTree as ET
from bs4 import BeautifulSoup
import time
import random
from sqlalchemy import func
import qrcode
from io import BytesIO
import base64
from flask_wtf.csrf import CSRFProtect
from requests_oauthlib import OAuth2Session
from dotenv import load_dotenv

# بارگذاری متغیرهای محیطی
load_dotenv()

app = Flask(__name__)
app.config['SECRET_KEY'] = os.getenv('FLASK_SECRET_KEY', 'xK9mP2nR5vQ8wE3tY7uI1oA4sD6fG0jL')
app.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite:///weather.db'
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
app.config['UPLOAD_FOLDER'] = 'uploads'
app.config['MAX_CONTENT_LENGTH'] = 100 * 1024 * 1024
app.config['SESSION_PERMANENT'] = False
app.config['PERMANENT_SESSION_LIFETIME'] = timedelta(hours=24)
app.config['WTF_CSRF_ENABLED'] = False

csrf = CSRFProtect(app)

# ===== فعال‌سازی HTTP برای محیط توسعه =====
os.environ['OAUTHLIB_INSECURE_TRANSPORT'] = '1'

# ======================== تنظیمات OAuth گوگل ========================
GOOGLE_CLIENT_ID = '513836943914-mjv4u40c1iast1ds1pu35ml2nk6m208p.apps.googleusercontent.com'
GOOGLE_CLIENT_SECRET = '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'
GOOGLE_REDIRECT_URI = 'https://weather-9000.onrender.com/google/callback'
GOOGLE_AUTHORIZATION_URL = 'https://accounts.google.com/o/oauth2/auth'
GOOGLE_TOKEN_URL = 'https://oauth2.googleapis.com/token'
GOOGLE_USERINFO_URL = 'https://www.googleapis.com/oauth2/v2/userinfo'

ALLOWED_EXTENSIONS = {'png', 'jpg', 'jpeg', 'gif', 'txt', 'pdf', 'doc', 'docx', 'xlsx', 'xls', 'csv'}

db = SQLAlchemy(app)
login_manager = LoginManager(app)
login_manager.login_view = 'login'
login_manager.login_message = 'لطفاً ابتدا وارد شوید'

# ======================== مدل‌های دیتابیس ========================
class User(UserMixin, db.Model):
    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(80), unique=True, nullable=False)
    password_hash = db.Column(db.String(200), nullable=True)
    email = db.Column(db.String(120), unique=True, nullable=False)
    is_active = db.Column(db.Boolean, default=True)
    is_admin = db.Column(db.Boolean, default=False)
    account_type = db.Column(db.String(20), default='basic')
    google_id = db.Column(db.String(100), unique=True, nullable=True)
    profile_picture = db.Column(db.String(300), nullable=True)
    full_name = db.Column(db.String(150), nullable=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    messages = db.relationship('Message', foreign_keys='Message.user_id', backref='user', lazy=True, cascade="all, delete-orphan")
    admin_messages = db.relationship('Message', foreign_keys='Message.admin_id', backref='admin_user', lazy=True, cascade="all, delete-orphan")
    orders = db.relationship('Order', backref='user', lazy=True)

    def set_password(self, password):
        self.password_hash = generate_password_hash(password)

    def check_password(self, password):
        return check_password_hash(self.password_hash, password)

class Setting(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    api_key = db.Column(db.String(200), default='010fc89b-8f88-4d84-8e3a-37cbb4d95fb9')
    base_url = db.Column(db.String(200), default='https://aki.io/openai/v1/chat/completions')
    model = db.Column(db.String(50), default='qwen3.6-35b')
    wallet_address = db.Column(db.String(200), default='0xa208B6474D6c549bcD0A8D6587CDD88c3e8EA62b')

class Message(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    admin_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=True)
    content = db.Column(db.Text, nullable=False)
    is_from_user = db.Column(db.Boolean, default=True)
    is_read = db.Column(db.Boolean, default=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    is_deleted_by_admin = db.Column(db.Boolean, default=False)

class Order(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    order_id = db.Column(db.String(50), unique=True, nullable=False)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    amount_usdt = db.Column(db.Float, nullable=False)
    wallet_address = db.Column(db.String(200), nullable=False)
    txid = db.Column(db.String(200), unique=True, nullable=True)
    status = db.Column(db.String(20), default='pending')
    account_type = db.Column(db.String(20), nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    paid_at = db.Column(db.DateTime, nullable=True)

@login_manager.user_loader
def load_user(user_id):
    return User.query.get(int(user_id))

# ======================== دکوریتورها ========================
def admin_required(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if not current_user.is_authenticated or not current_user.is_admin:
            flash('دسترسی غیرمجاز', 'danger')
            return redirect(url_for('index'))
        return f(*args, **kwargs)
    return decorated_function

def premium_required(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if not current_user.is_authenticated:
            flash('لطفاً وارد شوید', 'warning')
            return redirect(url_for('login'))
        if current_user.account_type not in ['premium', 'vip'] and not current_user.is_admin:
            flash('این بخش نیاز به اشتراک Premium دارد', 'warning')
            return redirect(url_for('pricing'))
        return f(*args, **kwargs)
    return decorated_function

def vip_required(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if not current_user.is_authenticated:
            flash('لطفاً وارد شوید', 'warning')
            return redirect(url_for('login'))
        if current_user.account_type != 'vip' and not current_user.is_admin:
            flash('این بخش نیاز به اشتراک VIP دارد', 'warning')
            return redirect(url_for('pricing'))
        return f(*args, **kwargs)
    return decorated_function

# ======================== توابع کمکی ========================
def get_settings():
    setting = Setting.query.first()
    if not setting:
        setting = Setting()
        db.session.add(setting)
        db.session.commit()
    return setting

def get_account_type_label(account_type):
    labels = {'basic': 'ساده', 'premium': 'پریمیوم', 'vip': 'VIP'}
    return labels.get(account_type, 'نامشخص')

def generate_order_id():
    return f"ORD-{datetime.now().strftime('%Y%m%d')}-{random.randint(1000, 9999)}"

def generate_qr_code(data):
    """تولید QR Code با پشتیبانی از هر نوع داده"""
    try:
        qr = qrcode.QRCode(
            version=1,
            error_correction=qrcode.constants.ERROR_CORRECT_L,
            box_size=8,
            border=2,
        )
        qr.add_data(data)
        qr.make(fit=True)
        
        img = qr.make_image(fill_color="black", back_color="white")
        buffered = BytesIO()
        img.save(buffered, format="PNG")
        return base64.b64encode(buffered.getvalue()).decode('utf-8')
    except Exception as e:
        print(f"❌ خطا در تولید QR Code: {e}")
        # ایجاد QR Code ساده در صورت خطا
        return ""

def allowed_file(filename):
    return '.' in filename and filename.rsplit('.', 1)[1].lower() in ALLOWED_EXTENSIONS

def extract_text_from_file(filepath, filename):
    ext = filename.rsplit('.', 1)[1].lower()
    try:
        if ext == 'txt':
            with open(filepath, 'r', encoding='utf-8') as f:
                return f.read()
        elif ext == 'pdf':
            text = ""
            with open(filepath, 'rb') as f:
                reader = PyPDF2.PdfReader(f)
                if len(reader.pages) == 0:
                    return "فایل PDF خالی است"
                for page in reader.pages:
                    page_text = page.extract_text()
                    if page_text:
                        text += page_text + "\n"
            return text if text.strip() else "متنی از PDF استخراج نشد"
        elif ext in {'doc', 'docx'}:
            try:
                import docx
                doc = docx.Document(filepath)
                text = "\n".join([para.text for para in doc.paragraphs])
                return text if text.strip() else "متنی از فایل استخراج نشد"
            except ImportError:
                return "برای پشتیبانی از فایل‌های DOCX، کتابخانه python-docx را نصب کنید."
        elif ext in {'xlsx', 'xls'}:
            try:
                df = pd.read_excel(filepath)
                return df.to_string()
            except Exception as e:
                return f"خطا در خواندن اکسل: {str(e)}"
        elif ext == 'csv':
            try:
                df = pd.read_csv(filepath, encoding='utf-8')
                return df.to_string()
            except:
                try:
                    df = pd.read_csv(filepath, encoding='ansi')
                    return df.to_string()
                except Exception as e:
                    return f"خطا در خواندن CSV: {str(e)}"
        elif ext in {'png', 'jpg', 'jpeg', 'gif'}:
            return "IMAGE_FILE"
        else:
            return "فرمت فایل پشتیبانی می‌شود اما استخراج متن ممکن نیست"
    except Exception as e:
        return f"خطا در استخراج متن: {str(e)}"

def get_weather_historical(lat, lon, date):
    """دریافت داده‌های تاریخی هواشناسی از Open-Meteo Archive API"""
    url = "https://archive-api.open-meteo.com/v1/archive"
    try:
        selected_date = datetime.strptime(date, '%Y-%m-%d')
        today = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
        if selected_date > today:
            date = today.strftime('%Y-%m-%d')
        if selected_date.year < 1940:
            date = '1940-01-01'
    except Exception:
        date = datetime.now().strftime('%Y-%m-%d')
    
    params = {
        "latitude": lat,
        "longitude": lon,
        "start_date": date,
        "end_date": date,
        "timezone": "auto",
        "daily": "temperature_2m_max,temperature_2m_min,precipitation_sum,wind_speed_10m_max,relative_humidity_2m_mean"
    }
    try:
        response = requests.get(url, params=params, timeout=30)
        response.raise_for_status()
        return response.json()
    except Exception as e:
        print(f"❌ خطا در دریافت داده تاریخی: {e}")
        return get_weather_forecast(lat, lon)

def get_weather_forecast(lat, lon):
    """دریافت پیش‌بینی ۷ روزه از Open-Meteo Forecast API"""
    url = "https://api.open-meteo.com/v1/forecast"
    params = {
        "latitude": lat,
        "longitude": lon,
        "timezone": "auto",
        "forecast_days": 7,
        "daily": "temperature_2m_max,temperature_2m_min,precipitation_sum,wind_speed_10m_max,relative_humidity_2m_mean"
    }
    try:
        response = requests.get(url, params=params, timeout=30)
        response.raise_for_status()
        return response.json()
    except Exception as e:
        print(f"❌ خطا در دریافت پیش‌بینی: {e}")
        return {"error": str(e)}

def get_open_meteo_current(lat, lon):
    """دریافت آب و هوای فعلی از Open-Meteo - مشابه Samsung Weather"""
    url = "https://api.open-meteo.com/v1/forecast"
    params = {
        "latitude": lat,
        "longitude": lon,
        "current": "temperature_2m,relative_humidity_2m,precipitation,wind_speed_10m,weather_code,pressure_msl,visibility,cloud_cover",
        "hourly": "temperature_2m,relative_humidity_2m,precipitation,wind_speed_10m,weather_code",
        "daily": "temperature_2m_max,temperature_2m_min,weather_code",
        "timezone": "auto",
        "forecast_days": 7,
        "past_days": 0
    }
    try:
        response = requests.get(url, params=params, timeout=30)
        response.raise_for_status()
        return response.json()
    except Exception as e:
        print(f"❌ خطا در دریافت آب و هوای فعلی: {e}")
        return {"error": str(e)}

def get_weather_by_city(city_name):
    """دریافت آب و هوای شهر با استفاده از Open-Meteo Geocoding + Forecast"""
    try:
        # 1. دریافت مختصات شهر
        geo_url = "https://geocoding-api.open-meteo.com/v1/search"
        geo_params = {
            "name": city_name,
            "count": 1,
            "language": "fa",
            "format": "json"
        }
        geo_response = requests.get(geo_url, params=geo_params, timeout=10)
        geo_response.raise_for_status()
        geo_data = geo_response.json()
        
        if not geo_data.get('results'):
            return {'error': f'شهر "{city_name}" یافت نشد', 'city': city_name}
        
        result = geo_data['results'][0]
        lat = result['latitude']
        lon = result['longitude']
        display_name = result.get('name', city_name)
        country = result.get('country', '')
        
        # 2. دریافت آب و هوای فعلی
        weather_url = "https://api.open-meteo.com/v1/forecast"
        weather_params = {
            "latitude": lat,
            "longitude": lon,
            "current": "temperature_2m,relative_humidity_2m,precipitation,wind_speed_10m,weather_code,pressure_msl,visibility,cloud_cover",
            "daily": "temperature_2m_max,temperature_2m_min,weather_code",
            "timezone": "auto",
            "forecast_days": 7,
            "past_days": 0
        }
        weather_response = requests.get(weather_url, params=weather_params, timeout=15)
        weather_response.raise_for_status()
        weather_data = weather_response.json()
        
        current = weather_data.get('current', {})
        daily = weather_data.get('daily', {})
        
        # 3. نقشه کد آب و هوا به شرایط
        weather_code = current.get('weather_code', 0)
        condition_map = {
            0: ('☀️', 'آفتابی'),
            1: ('🌤️', 'کمی ابری'),
            2: ('⛅', 'نیمه ابری'),
            3: ('☁️', 'ابری'),
            45: ('🌫️', 'مه‌آلود'),
            48: ('🌫️', 'مه‌آلود'),
            51: ('🌧️', 'نم نم باران'),
            53: ('🌧️', 'باران ملایم'),
            55: ('🌧️', 'باران شدید'),
            61: ('🌧️', 'باران ملایم'),
            63: ('🌧️', 'باران متوسط'),
            65: ('🌧️', 'باران شدید'),
            71: ('❄️', 'برف ملایم'),
            73: ('❄️', 'برف متوسط'),
            75: ('❄️', 'برف شدید'),
            80: ('🌧️', 'رگبار'),
            81: ('🌧️', 'رگبار متوسط'),
            82: ('🌧️', 'رگبار شدید'),
            95: ('⛈️', 'رعد و برق'),
            96: ('⛈️', 'رعد و برق با تگرگ'),
            99: ('⛈️', 'رعد و برق شدید')
        }
        icon, condition = condition_map.get(weather_code, ('🌤️', 'نامشخص'))
        
        # 4. ساخت داده‌های پیش‌بینی ۷ روزه
        forecast_data = []
        daily_times = daily.get('time', [])
        daily_max = daily.get('temperature_2m_max', [])
        daily_min = daily.get('temperature_2m_min', [])
        daily_code = daily.get('weather_code', [])
        
        for i in range(min(7, len(daily_times))):
            if i < len(daily_times) and i < len(daily_max) and i < len(daily_min):
                code = daily_code[i] if i < len(daily_code) else 0
                icon_f, cond_f = condition_map.get(code, ('🌤️', 'نامشخص'))
                forecast_data.append({
                    'date': daily_times[i],
                    'temp_max': daily_max[i] if daily_max[i] is not None else '--',
                    'temp_min': daily_min[i] if daily_min[i] is not None else '--',
                    'condition': cond_f,
                    'icon': icon_f
                })
        
        # 5. داده‌های ساعتی برای ۲۴ ساعت آینده
        hourly_data = []
        hourly_times = weather_data.get('hourly', {}).get('time', [])
        hourly_temp = weather_data.get('hourly', {}).get('temperature_2m', [])
        hourly_code = weather_data.get('hourly', {}).get('weather_code', [])
        
        for i in range(min(24, len(hourly_times))):
            if i < len(hourly_times) and i < len(hourly_temp):
                code = hourly_code[i] if i < len(hourly_code) else 0
                icon_f, cond_f = condition_map.get(code, ('🌤️', 'نامشخص'))
                hourly_data.append({
                    'time': hourly_times[i],
                    'temp': hourly_temp[i] if hourly_temp[i] is not None else '--',
                    'condition': cond_f,
                    'icon': icon_f
                })
        
        # 6. اطلاعات تکمیلی
        temp = current.get('temperature_2m')
        humidity = current.get('relative_humidity_2m')
        pressure = current.get('pressure_msl')
        visibility = current.get('visibility')
        wind_speed = current.get('wind_speed_10m')
        precipitation = current.get('precipitation')
        cloud_cover = current.get('cloud_cover')
        
        weather_data_result = {
            'city': display_name,
            'country': country,
            'temperature': f"{temp}°C" if temp is not None else '--',
            'temp_value': temp,
            'humidity': f"{humidity}%" if humidity is not None else '--',
            'humidity_value': humidity,
            'condition': condition,
            'condition_icon': icon,
            'pressure': f"{pressure} hPa" if pressure is not None else '--',
            'visibility': f"{visibility}m" if visibility is not None else '--',
            'wind_speed': f"{wind_speed} km/h" if wind_speed is not None else '--',
            'precipitation': f"{precipitation} mm" if precipitation is not None else '--',
            'cloud_cover': f"{cloud_cover}%" if cloud_cover is not None else '--',
            'source': 'Open-Meteo',
            'forecast': forecast_data,
            'hourly': hourly_data,
            'lat': lat,
            'lon': lon
        }
        return weather_data_result
        
    except Exception as e:
        print(f"❌ خطا در دریافت آب و هوا: {e}")
        return {'error': str(e), 'city': city_name}

def get_weather_news_iran():
    news_items = []
    try:
        url = "https://www.shahrekhabar.com/tag/%D9%87%D9%88%D8%A7%D8%B4%D9%86%D8%A7%D8%B3%DB%8C"
        headers = {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
            'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8'
        }
        response = requests.get(url, headers=headers, timeout=20)
        if response.status_code == 200:
            soup = BeautifulSoup(response.text, 'html.parser')
            articles = soup.find_all('article') or soup.find_all('div', class_=re.compile('.*item.*|.*news.*|.*post.*', re.I))
            for article in articles[:10]:
                title_elem = article.find('h2') or article.find('h3') or article.find('h1')
                if not title_elem:
                    title_elem = article.find('a')
                if title_elem:
                    title = title_elem.text.strip()
                    link = title_elem.get('href') if title_elem.name == 'a' else None
                    if not link:
                        link_elem = article.find('a')
                        if link_elem:
                            link = link_elem.get('href')
                    if link and not link.startswith('http'):
                        link = 'https://www.shahrekhabar.com' + link
                    if title and len(title) > 10:
                        news_items.append({
                            'title': title[:150],
                            'link': link or '#',
                            'source': 'شهرخبر'
                        })
    except Exception as e:
        print(f"⚠️ خطا در دریافت اخبار شهرخبر: {e}")
    return news_items

def get_weather_news_euronews():
    news_items = []
    try:
        url = "https://parsi.euronews.com/weather"
        headers = {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
            'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8'
        }
        response = requests.get(url, headers=headers, timeout=20)
        if response.status_code == 200:
            soup = BeautifulSoup(response.text, 'html.parser')
            articles = soup.find_all('article') or soup.find_all('div', class_=re.compile('.*story.*|.*card.*|.*item.*|.*post.*', re.I))
            for article in articles[:10]:
                title_elem = article.find('h2') or article.find('h3') or article.find('h1')
                if not title_elem:
                    title_elem = article.find('a')
                if title_elem:
                    title = title_elem.text.strip()
                    link = title_elem.get('href') if title_elem.name == 'a' else None
                    if not link:
                        link_elem = article.find('a')
                        if link_elem:
                            link = link_elem.get('href')
                    if link and not link.startswith('http'):
                        link = 'https://parsi.euronews.com' + link
                    if title and len(title) > 10:
                        news_items.append({
                            'title': title[:150],
                            'link': link or '#',
                            'source': 'یورونیوز'
                        })
    except Exception as e:
        print(f"⚠️ خطا در دریافت اخبار یورونیوز: {e}")
    return news_items

def get_weather_news_combined():
    all_news = []
    iran_news = get_weather_news_iran()
    for item in iran_news:
        item['title'] = '🇮🇷 ' + item['title']
        all_news.append(item)
    global_news = get_weather_news_euronews()
    for item in global_news:
        item['title'] = '🌍 ' + item['title']
        all_news.append(item)
    if not all_news:
        all_news = get_weather_news_fallback()
    return all_news[:20]

def get_weather_news_fallback():
    news_items = []
    try:
        url = "https://news.google.com/rss/search?q=weather&hl=en&gl=US&ceid=US:en"
        headers = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'}
        response = requests.get(url, headers=headers, timeout=15)
        if response.status_code == 200:
            soup = BeautifulSoup(response.text, 'xml')
            items = soup.find_all('item')[:15]
            for item in items:
                title = item.title.text if item.title else ''
                link = item.link.text if item.link else '#'
                if title and len(title) > 5:
                    news_items.append({
                        'title': '🌍 ' + title[:150],
                        'link': link,
                        'source': 'Google News'
                    })
    except Exception as e:
        print(f"⚠️ خطا در اخبار جایگزین: {e}")
    return news_items

def chat_with_ai(messages):
    settings = get_settings()
    headers = {"Authorization": f"Bearer {settings.api_key}", "Content-Type": "application/json"}
    data = {"model": settings.model, "messages": messages, "temperature": 0.2, "max_tokens": 10000}
    try:
        response = requests.post(f"{settings.base_url}", headers=headers, json=data, timeout=180)
        response.raise_for_status()
        return response.json()
    except Exception as e:
        return {"error": str(e)}

def search_location(query):
    url = "https://nominatim.openstreetmap.org/search"
    params = {"q": query, "format": "json", "limit": 20, "accept-language": "fa"}
    headers = {"User-Agent": "WeatherApp/1.0"}
    try:
        response = requests.get(url, params=params, headers=headers, timeout=10)
        response.raise_for_status()
        data = response.json()
        results = []
        for item in data:
            display_name = item.get('display_name', '')
            name = item.get('name', '')
            clean_name = name
            prefixes = ['شهر', 'استان', 'روستا', 'دهستان', 'بخش', 'شهرستان', 'منطقه']
            for prefix in prefixes:
                if clean_name.startswith(prefix + ' '):
                    clean_name = clean_name[len(prefix) + 1:]
                    break
            if not clean_name:
                clean_name = name
            if not clean_name and display_name:
                clean_name = display_name.split(',')[0]
            results.append({
                'lat': item.get('lat'),
                'lon': item.get('lon'),
                'display_name': display_name,
                'name': clean_name
            })
        return results
    except Exception as e:
        print(f"❌ خطا در جستجو: {e}")
        return []

def clean_and_parse_json(text):
    if not text:
        return None
    text = text.strip()
    text = re.sub(r'```json\s*', '', text)
    text = re.sub(r'```\s*', '', text)
    text = re.sub(r'`', '', text)
    json_match = re.search(r'\{.*\}', text, re.DOTALL)
    if json_match:
        try:
            return json.loads(json_match.group())
        except:
            pass
    try:
        return json.loads(text)
    except:
        pass
    return None

def parse_ai_response(content):
    try:
        if isinstance(content, dict):
            text = content.get('text', '')
            visual = content.get('visual', [])
            if isinstance(visual, str):
                try:
                    visual = json.loads(visual)
                except:
                    visual = []
            if isinstance(visual, dict):
                visual = [visual]
            if not isinstance(visual, list):
                visual = []
            validated_visual = []
            for item in visual:
                if isinstance(item, dict):
                    if 'type' not in item:
                        continue
                    if item.get('type') == 'table':
                        if 'headers' in item and 'rows' in item:
                            validated_visual.append(item)
                    elif item.get('type') == 'chart':
                        if 'chartType' in item and 'labels' in item and 'datasets' in item:
                            validated_visual.append(item)
            return text, validated_visual
        if isinstance(content, str):
            data = clean_and_parse_json(content)
            if data and isinstance(data, dict):
                text = data.get('text', '')
                visual = data.get('visual', [])
                if isinstance(visual, str):
                    try:
                        visual = json.loads(visual)
                    except:
                        visual = []
                if isinstance(visual, dict):
                    visual = [visual]
                if not isinstance(visual, list):
                    visual = []
                validated_visual = []
                for item in visual:
                    if isinstance(item, dict):
                        if 'type' not in item:
                            continue
                        if item.get('type') == 'table':
                            if 'headers' in item and 'rows' in item:
                                validated_visual.append(item)
                        elif item.get('type') == 'chart':
                            if 'chartType' in item and 'labels' in item and 'datasets' in item:
                                validated_visual.append(item)
                return text, validated_visual
            return content, []
        return str(content), []
    except Exception as e:
        print(f"❌ خطا در parse_ai_response: {e}")
        return str(content), []

def predict_missing_value(data, index):
    if not data or index < 0 or index >= len(data):
        return None
    clean_data = []
    for val in data:
        if val is not None and val != '' and val != 'NON' and str(val).lower() != 'non':
            try:
                clean_data.append(float(val))
            except:
                clean_data.append(None)
        else:
            clean_data.append(None)
    if clean_data[index] is not None:
        return clean_data[index]
    prev_values = []
    next_values = []
    for i in range(index - 1, -1, -1):
        if clean_data[i] is not None:
            prev_values.append(clean_data[i])
            if len(prev_values) >= 3:
                break
    for i in range(index + 1, len(clean_data)):
        if clean_data[i] is not None:
            next_values.append(clean_data[i])
            if len(next_values) >= 3:
                break
    if prev_values and next_values:
        return (sum(prev_values) / len(prev_values) + sum(next_values) / len(next_values)) / 2
    if prev_values:
        return sum(prev_values) / len(prev_values)
    if next_values:
        return sum(next_values) / len(next_values)
    return None

def parse_weather_data_for_ai(weather_data):
    dates = weather_data.get('dates', [])
    temp_max = weather_data.get('temp_max', [])
    temp_min = weather_data.get('temp_min', [])
    precipitation = weather_data.get('precipitation', [])
    wind_speed = weather_data.get('wind_speed', [])
    humidity = weather_data.get('humidity', [])
    min_len = min(len(dates), len(temp_max), len(temp_min), len(precipitation), len(wind_speed), len(humidity))
    dates = dates[:min_len]
    temp_max = temp_max[:min_len]
    temp_min = temp_min[:min_len]
    precipitation = precipitation[:min_len]
    wind_speed = wind_speed[:min_len]
    humidity = humidity[:min_len]
    for i in range(len(temp_max)):
        if temp_max[i] is None or temp_max[i] == 'NON':
            temp_max[i] = predict_missing_value(temp_max, i)
        if temp_min[i] is None or temp_min[i] == 'NON':
            temp_min[i] = predict_missing_value(temp_min, i)
        if precipitation[i] is None or precipitation[i] == 'NON':
            precipitation[i] = predict_missing_value(precipitation, i)
        if wind_speed[i] is None or wind_speed[i] == 'NON':
            wind_speed[i] = predict_missing_value(wind_speed, i)
        if humidity[i] is None or humidity[i] == 'NON':
            humidity[i] = predict_missing_value(humidity, i)
    return {
        'dates': dates,
        'temp_max': [float(x) if x is not None else None for x in temp_max],
        'temp_min': [float(x) if x is not None else None for x in temp_min],
        'precipitation': [float(x) if x is not None else None for x in precipitation],
        'wind_speed': [float(x) if x is not None else None for x in wind_speed],
        'humidity': [float(x) if x is not None else None for x in humidity]
    }

# ======================== روت‌های اصلی ========================
@app.route('/')
@login_required
def index():
    today = datetime.today().strftime('%Y-%m-%d')
    return render_template('index.html', today=today, user=current_user)

@app.route('/login', methods=['GET', 'POST'])
def login():
    if current_user.is_authenticated:
        return redirect(url_for('index'))
    
    if request.method == 'POST':
        username = request.form.get('username', '').strip()
        password = request.form.get('password', '')
        
        if not username or not password:
            flash('لطفاً نام کاربری و رمز عبور را وارد کنید', 'warning')
            return render_template('login.html')
        
        user = User.query.filter_by(username=username).first()
        if user and user.check_password(password):
            if not user.is_active:
                flash('حساب کاربری غیرفعال است', 'warning')
                return render_template('login.html')
            login_user(user)
            flash('خوش آمدید!', 'success')
            return redirect(url_for('index'))
        else:
            flash('نام کاربری یا رمز عبور اشتباه است', 'danger')
    
    return render_template('login.html')

@app.route('/google/login')
def google_login():
    oauth = OAuth2Session(
        GOOGLE_CLIENT_ID,
        redirect_uri=GOOGLE_REDIRECT_URI,
        scope=['openid', 'email', 'profile']
    )
    authorization_url, state = oauth.authorization_url(
        GOOGLE_AUTHORIZATION_URL,
        access_type='offline',
        prompt='select_account'
    )
    session['oauth_state'] = state
    return redirect(authorization_url)

@app.route('/google/callback')
def google_callback():
    try:
        oauth = OAuth2Session(
            GOOGLE_CLIENT_ID,
            state=session.get('oauth_state'),
            redirect_uri=GOOGLE_REDIRECT_URI
        )
        
        token = oauth.fetch_token(
            GOOGLE_TOKEN_URL,
            client_secret=GOOGLE_CLIENT_SECRET,
            authorization_response=request.url
        )
        
        user_info_response = oauth.get(GOOGLE_USERINFO_URL)
        user_info = user_info_response.json()
        
        if not user_info or not user_info.get('email'):
            flash('خطا در دریافت اطلاعات از گوگل', 'danger')
            return redirect(url_for('login'))
        
        email = user_info.get('email')
        full_name = user_info.get('name', '')
        picture = user_info.get('picture', '')
        google_id = user_info.get('id')
        
        user = User.query.filter_by(email=email).first()
        
        if not user:
            username = email.split('@')[0]
            if User.query.filter_by(username=username).first():
                username = f"{username}_{random.randint(1000, 9999)}"
            
            user = User(
                username=username,
                email=email,
                full_name=full_name,
                google_id=google_id,
                profile_picture=picture,
                is_active=True,
                account_type='basic'
            )
            db.session.add(user)
            db.session.commit()
            flash('ثبت نام با گوگل موفق بود! 🎉', 'success')
        else:
            if not user.google_id:
                user.google_id = google_id
            if full_name:
                user.full_name = full_name
            if picture:
                user.profile_picture = picture
            db.session.commit()
            flash('خوش آمدید! 👋', 'success')
        
        login_user(user)
        return redirect(url_for('index'))
        
    except Exception as e:
        print(f"❌ خطا در Google Callback: {e}")
        traceback.print_exc()
        flash(f'خطا در ورود با گوگل: {str(e)}', 'danger')
        return redirect(url_for('login'))

@app.route('/register', methods=['GET', 'POST'])
def register():
    if current_user.is_authenticated:
        return redirect(url_for('index'))
    
    if request.method == 'POST':
        username = request.form.get('username', '').strip()
        password = request.form.get('password', '')
        email = request.form.get('email', '').strip()
        
        if not username or not password or not email:
            flash('لطفاً تمام فیلدها را پر کنید', 'warning')
            return render_template('register.html')
        
        if User.query.filter_by(username=username).first():
            flash('این نام کاربری قبلاً ثبت شده است', 'danger')
            return render_template('register.html')
        
        if User.query.filter_by(email=email).first():
            flash('این ایمیل قبلاً ثبت شده است', 'danger')
            return render_template('register.html')
        
        user = User(username=username, email=email, is_active=False, account_type='basic')
        user.set_password(password)
        db.session.add(user)
        db.session.commit()
        
        flash('ثبت نام با موفقیت انجام شد. پس از تایید ادمین وارد شوید.', 'success')
        return redirect(url_for('login'))
    
    return render_template('register.html')

@app.route('/logout')
@login_required
def logout():
    logout_user()
    flash('خارج شدید', 'info')
    return redirect(url_for('login'))

# ======================== پنل ادمین ========================
@app.route('/admin')
@login_required
@admin_required
def admin():
    settings = get_settings()
    users = User.query.all()
    orders = Order.query.order_by(Order.created_at.desc()).all()
    
    user_message_counts = {}
    for user in users:
        count = Message.query.filter_by(user_id=user.id, is_read=False, is_from_user=True).count()
        user_message_counts[user.id] = count
    
    return render_template('admin.html', settings=settings, users=users, 
                          user_message_counts=user_message_counts, orders=orders)

@app.route('/admin/settings', methods=['POST'])
@login_required
@admin_required
def admin_settings():
    setting = get_settings()
    setting.api_key = request.form.get('api_key', '').strip()
    setting.base_url = request.form.get('base_url', '').strip()
    setting.model = request.form.get('model', '').strip()
    setting.wallet_address = request.form.get('wallet_address', '').strip()
    db.session.commit()
    flash('تنظیمات ذخیره شد', 'success')
    return redirect(url_for('admin'))

@app.route('/admin/users/<int:user_id>/toggle', methods=['POST'])
@login_required
@admin_required
def toggle_user(user_id):
    try:
        user = User.query.get_or_404(user_id)
        if user.is_admin:
            return jsonify({'success': False, 'error': 'نمی‌توان ادمین را تغییر داد'}), 403
        user.is_active = not user.is_active
        db.session.commit()
        return jsonify({'success': True, 'message': 'وضعیت کاربر تغییر کرد', 'is_active': user.is_active})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500

@app.route('/admin/users/<int:user_id>/account-type', methods=['POST'])
@login_required
@admin_required
def admin_change_account_type(user_id):
    try:
        user = User.query.get_or_404(user_id)
        if user.is_admin:
            flash('نمی‌توان ادمین را تغییر داد', 'danger')
            return redirect(url_for('admin'))
        
        account_type = request.form.get('account_type')
        if account_type in ['basic', 'premium', 'vip']:
            user.account_type = account_type
            db.session.commit()
            flash(f'نوع اکانت کاربر به {get_account_type_label(account_type)} تغییر کرد', 'success')
        else:
            flash('نوع اکانت نامعتبر است', 'danger')
        
        return redirect(url_for('admin'))
    except Exception as e:
        flash(f'خطا: {str(e)}', 'danger')
        return redirect(url_for('admin'))

@app.route('/admin/users/<int:user_id>/delete', methods=['POST'])
@login_required
@admin_required
def delete_user(user_id):
    try:
        user = User.query.get_or_404(user_id)
        if user.is_admin:
            return jsonify({'success': False, 'error': 'نمی‌توان ادمین را حذف کرد'}), 403
        username = user.username
        db.session.delete(user)
        db.session.commit()
        return jsonify({'success': True, 'message': f'کاربر {username} حذف شد'})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500

# ======================== API پیام‌ها ========================
@app.route('/api/admin/messages/<int:user_id>', methods=['GET'])
@login_required
@admin_required
def api_get_messages(user_id):
    try:
        messages = Message.query.filter_by(user_id=user_id).order_by(Message.created_at.asc()).all()
        unread_messages = Message.query.filter_by(user_id=user_id, is_read=False, is_from_user=True).all()
        for msg in unread_messages:
            msg.is_read = True
        db.session.commit()
        
        result = []
        for msg in messages:
            if msg.is_from_user and not msg.is_deleted_by_admin:
                result.append({
                    'id': msg.id,
                    'content': msg.content,
                    'is_from_user': True,
                    'created_at': msg.created_at.isoformat()
                })
            elif not msg.is_from_user and not msg.is_deleted_by_admin:
                result.append({
                    'id': msg.id,
                    'content': msg.content,
                    'is_from_user': False,
                    'created_at': msg.created_at.isoformat()
                })
        return jsonify(result)
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/api/admin/messages/send', methods=['POST'])
@login_required
@admin_required
def api_send_message():
    try:
        data = request.json
        user_id = data.get('user_id')
        content = data.get('content', '').strip()
        
        if not user_id or not content:
            return jsonify({'error': 'مشخصات ناقص است'}), 400
        
        message = Message(
            user_id=user_id,
            admin_id=current_user.id,
            content=content,
            is_from_user=False,
            is_read=True
        )
        db.session.add(message)
        db.session.commit()
        
        return jsonify({
            'id': message.id,
            'content': message.content,
            'is_from_user': False,
            'created_at': message.created_at.isoformat()
        })
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/api/admin/messages/delete/<int:message_id>', methods=['DELETE'])
@login_required
@admin_required
def api_delete_message(message_id):
    try:
        message = Message.query.get_or_404(message_id)
        if message.is_from_user:
            return jsonify({'error': 'نمی‌توان پیام کاربر را حذف کرد'}), 403
        message.is_deleted_by_admin = True
        db.session.commit()
        return jsonify({'success': True})
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/api/admin/broadcast', methods=['POST'])
@login_required
@admin_required
def api_broadcast():
    try:
        data = request.get_json()
        if not data:
            return jsonify({'error': 'درخواست نامعتبر'}), 400
        
        content = data.get('content', '').strip()
        if not content:
            return jsonify({'error': 'متن اعلان نمی‌تواند خالی باشد'}), 400
        
        users = User.query.filter_by(is_active=True).all()
        for user in users:
            message = Message(
                user_id=user.id,
                admin_id=current_user.id,
                content=f"📢 اعلان همگانی: {content}",
                is_from_user=False,
                is_read=False
            )
            db.session.add(message)
        db.session.commit()
        return jsonify({'success': True, 'message': f'اعلان به {len(users)} کاربر ارسال شد'})
    except Exception as e:
        print(f"❌ خطا: {e}")
        traceback.print_exc()
        return jsonify({'error': str(e)}), 500

@app.route('/api/user/messages', methods=['GET'])
@login_required
def api_get_user_messages():
    try:
        messages = Message.query.filter_by(user_id=current_user.id).order_by(Message.created_at.asc()).all()
        result = []
        for msg in messages:
            if msg.is_from_user or (not msg.is_from_user and not msg.is_deleted_by_admin):
                result.append({
                    'id': msg.id,
                    'content': msg.content,
                    'is_from_user': msg.is_from_user,
                    'created_at': msg.created_at.isoformat()
                })
        return jsonify(result)
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/api/user/messages/send', methods=['POST'])
@login_required
def api_user_send_message():
    try:
        data = request.get_json()
        if not data:
            return jsonify({'error': 'درخواست نامعتبر'}), 400
        
        content = data.get('content', '').strip()
        if not content:
            return jsonify({'error': 'متن پیام نمی‌تواند خالی باشد'}), 400
        
        message = Message(
            user_id=current_user.id,
            content=content,
            is_from_user=True,
            is_read=False
        )
        db.session.add(message)
        db.session.commit()
        
        return jsonify({
            'id': message.id,
            'content': message.content,
            'is_from_user': True,
            'created_at': message.created_at.isoformat()
        })
    except Exception as e:
        return jsonify({'error': str(e)}), 500

# ======================== APIهای اصلی ========================
@app.route('/api/search')
@login_required
def api_search():
    try:
        query = request.args.get('q', '').strip()
        if not query or len(query) < 2:
            return jsonify([])
        results = search_location(query)
        return jsonify(results)
    except Exception as e:
        return jsonify([])

@app.route('/api/files')
@login_required
def api_get_files():
    try:
        files = session.get('uploaded_files', [])
        return jsonify([{'filename': f['filename']} for f in files])
    except:
        return jsonify([])

@app.route('/api/files/delete', methods=['POST'])
@login_required
def api_delete_file():
    try:
        data = request.get_json()
        if not data:
            return jsonify({'error': 'درخواست نامعتبر'}), 400
        
        filename = data.get('filename', '')
        if not filename:
            return jsonify({'error': 'نام فایل مشخص نشده'}), 400
        files = session.get('uploaded_files', [])
        files = [f for f in files if f['filename'] != filename]
        session['uploaded_files'] = files
        session.modified = True
        filepath = os.path.join(app.config['UPLOAD_FOLDER'], filename)
        if os.path.exists(filepath):
            os.remove(filepath)
        return jsonify({'success': True})
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/api/upload', methods=['POST'])
@login_required
def api_upload():
    try:
        if 'file' not in request.files:
            return jsonify({'error': 'فایلی ارسال نشده'}), 400
        file = request.files['file']
        if file.filename == '':
            return jsonify({'error': 'نام فایل خالی است'}), 400
        if not allowed_file(file.filename):
            return jsonify({'error': 'فرمت فایل مجاز نیست'}), 400
        os.makedirs(app.config['UPLOAD_FOLDER'], exist_ok=True)
        filename = secure_filename(file.filename)
        filepath = os.path.join(app.config['UPLOAD_FOLDER'], filename)
        file.save(filepath)
        text = extract_text_from_file(filepath, filename)
        if 'uploaded_files' not in session:
            session['uploaded_files'] = []
        if len(session['uploaded_files']) >= 5:
            session['uploaded_files'] = session['uploaded_files'][-4:]
        session['uploaded_files'].append({'filename': filename, 'text': text[:50000]})
        session.modified = True
        return jsonify({'success': True, 'filename': filename})
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/api/weather/historical', methods=['POST'])
@login_required
@vip_required
def api_weather_historical():
    try:
        data = request.get_json()
        if not data:
            return jsonify({'error': 'درخواست نامعتبر'}), 400
        
        lat = data.get('lat')
        lon = data.get('lon')
        date = data.get('date')
        user_question = data.get('question', '').strip()
        
        if not all([lat, lon, date]):
            return jsonify({'error': 'مشخصات ناقص است'}), 400
        
        lat = float(lat)
        lon = float(lon)
        
        weather = get_weather_historical(lat, lon, date)
        if 'error' in weather:
            return jsonify({'error': weather['error']}), 500
        
        daily = weather.get('daily', {})
        times = daily.get('time', [])
        
        if not times:
            return jsonify({'error': 'داده‌ای برای تاریخ مورد نظر یافت نشد'}), 404
        
        temp_max_list = []
        temp_min_list = []
        precip_list = []
        wind_list = []
        humidity_list = []
        dates_list = []
        
        for i, d in enumerate(times):
            t_max = daily.get('temperature_2m_max', [])[i] if i < len(daily.get('temperature_2m_max', [])) else None
            t_min = daily.get('temperature_2m_min', [])[i] if i < len(daily.get('temperature_2m_min', [])) else None
            precip = daily.get('precipitation_sum', [])[i] if i < len(daily.get('precipitation_sum', [])) else None
            wind = daily.get('wind_speed_10m_max', [])[i] if i < len(daily.get('wind_speed_10m_max', [])) else None
            humid = daily.get('relative_humidity_2m_mean', [])[i] if i < len(daily.get('relative_humidity_2m_mean', [])) else None
            
            dates_list.append(d)
            temp_max_list.append(t_max)
            temp_min_list.append(t_min)
            precip_list.append(precip)
            wind_list.append(wind)
            humidity_list.append(humid)
        
        weather_data = {
            'dates': dates_list,
            'temp_max': temp_max_list,
            'temp_min': temp_min_list,
            'precipitation': precip_list,
            'wind_speed': wind_list,
            'humidity': humidity_list
        }
        
        processed_data = parse_weather_data_for_ai(weather_data)
        
        data_text = ""
        for i, d in enumerate(processed_data['dates']):
            t_max = processed_data['temp_max'][i] if i < len(processed_data['temp_max']) and processed_data['temp_max'][i] is not None else '-'
            t_min = processed_data['temp_min'][i] if i < len(processed_data['temp_min']) and processed_data['temp_min'][i] is not None else '-'
            precip = processed_data['precipitation'][i] if i < len(processed_data['precipitation']) and processed_data['precipitation'][i] is not None else '-'
            wind = processed_data['wind_speed'][i] if i < len(processed_data['wind_speed']) and processed_data['wind_speed'][i] is not None else '-'
            humid = processed_data['humidity'][i] if i < len(processed_data['humidity']) and processed_data['humidity'][i] is not None else '-'
            
            data_text += f"\n📅 {d}: دمای حداکثر {t_max}°C، حداقل {t_min}°C، بارش {precip}mm، باد {wind}km/h، رطوبت {humid}%"
        
        prompt = f"""
        داده‌های هواشناسی تاریخی برای تاریخ {date}:
        {data_text}
        
        داده‌های عددی (مقادیر گمشده پیش‌بینی شده‌اند):
        {json.dumps(processed_data, ensure_ascii=False, indent=2)}
        
        {f'سوال کاربر: {user_question}' if user_question else 'لطفاً یک تحلیل کامل و دقیق از داده‌های هواشناسی ارائه دهید.'}
        
        دستورالعمل‌ها:
        1. پاسخ را به صورت JSON با کلیدهای 'text' و 'visual' برگردان
        2. text: تحلیل کامل و دقیق به صورت پاراگراف‌بندی شده با توضیحات کامل
        3. visual: لیستی از جدول‌ها و/یا نمودارها (هر تعداد که کاربر خواسته باشد)
        
        ساختار visual (لیستی از آیتم‌ها):
        - جدول: {{"type": "table", "headers": ["ستون1", "ستون2", ...], "rows": [["مقدار1", "مقدار2", ...], ...]}}
        - نمودار خطی: {{"type": "chart", "chartType": "line", "title": "عنوان", "labels": ["برچسب1", ...], "datasets": [{{"label": "نام", "data": [مقدار1, ...], "borderColor": "رنگ"}}]}}
        - نمودار میله‌ای: {{"type": "chart", "chartType": "bar", "title": "عنوان", "labels": ["برچسب1", ...], "datasets": [{{"label": "نام", "data": [مقدار1, ...], "backgroundColor": "رنگ"}}]}}
        
        فقط JSON معتبر برگردان.
        """
        
        ai_response = chat_with_ai([
            {'role': 'system', 'content': 'شما متخصص هواشناسی با ۲۰ سال تجربه هستید. پاسخ را دقیقاً به صورت JSON با کلیدهای "text" و "visual" برگردانید. visual باید لیستی از جدول‌ها و نمودارها باشد.'},
            {'role': 'user', 'content': prompt}
        ])
        
        if 'error' in ai_response:
            return jsonify({'error': ai_response['error']}), 500
        
        content = ai_response['choices'][0]['message']['content']
        text, visual = parse_ai_response(content)
        
        if not isinstance(visual, list):
            visual = []
        
        return jsonify({
            'text': text or 'تحلیل انجام شد',
            'visual': visual
        })
        
    except Exception as e:
        print(f"❌ خطا: {e}")
        traceback.print_exc()
        return jsonify({'error': str(e)}), 500

@app.route('/api/weather/forecast', methods=['POST'])
@login_required
@premium_required
def api_weather_forecast():
    try:
        data = request.get_json()
        if not data:
            return jsonify({'error': 'درخواست نامعتبر'}), 400
        
        lat = data.get('lat')
        lon = data.get('lon')
        user_question = data.get('question', '').strip()
        
        if not all([lat, lon]):
            return jsonify({'error': 'مشخصات ناقص است'}), 400
        
        lat = float(lat)
        lon = float(lon)
        
        forecast = get_weather_forecast(lat, lon)
        if 'error' in forecast:
            return jsonify({'error': forecast['error']}), 500
        
        daily = forecast.get('daily', {})
        times = daily.get('time', [])
        
        if not times:
            return jsonify({'error': 'داده‌ای برای پیش‌بینی یافت نشد'}), 404
        
        temp_max_list = []
        temp_min_list = []
        precip_list = []
        wind_list = []
        humidity_list = []
        dates_list = []
        
        for i, d in enumerate(times):
            t_max = daily.get('temperature_2m_max', [])[i] if i < len(daily.get('temperature_2m_max', [])) else None
            t_min = daily.get('temperature_2m_min', [])[i] if i < len(daily.get('temperature_2m_min', [])) else None
            precip = daily.get('precipitation_sum', [])[i] if i < len(daily.get('precipitation_sum', [])) else None
            wind = daily.get('wind_speed_10m_max', [])[i] if i < len(daily.get('wind_speed_10m_max', [])) else None
            humid = daily.get('relative_humidity_2m_mean', [])[i] if i < len(daily.get('relative_humidity_2m_mean', [])) else None
            
            dates_list.append(d)
            temp_max_list.append(t_max)
            temp_min_list.append(t_min)
            precip_list.append(precip)
            wind_list.append(wind)
            humidity_list.append(humid)
        
        weather_data = {
            'dates': dates_list,
            'temp_max': temp_max_list,
            'temp_min': temp_min_list,
            'precipitation': precip_list,
            'wind_speed': wind_list,
            'humidity': humidity_list
        }
        
        processed_data = parse_weather_data_for_ai(weather_data)
        
        data_text = "پیش‌بینی ۷ روزه:\n"
        for i, d in enumerate(processed_data['dates']):
            t_max = processed_data['temp_max'][i] if i < len(processed_data['temp_max']) and processed_data['temp_max'][i] is not None else '-'
            t_min = processed_data['temp_min'][i] if i < len(processed_data['temp_min']) and processed_data['temp_min'][i] is not None else '-'
            precip = processed_data['precipitation'][i] if i < len(processed_data['precipitation']) and processed_data['precipitation'][i] is not None else '-'
            wind = processed_data['wind_speed'][i] if i < len(processed_data['wind_speed']) and processed_data['wind_speed'][i] is not None else '-'
            humid = processed_data['humidity'][i] if i < len(processed_data['humidity']) and processed_data['humidity'][i] is not None else '-'
            
            data_text += f"\n📅 {d}: حداکثر {t_max}°C، حداقل {t_min}°C، بارش {precip}mm، باد {wind}km/h، رطوبت {humid}%"
        
        prompt = f"""
        {data_text}
        
        داده‌های عددی (مقادیر گمشده پیش‌بینی شده‌اند):
        {json.dumps(processed_data, ensure_ascii=False, indent=2)}
        
        {f'سوال کاربر: {user_question}' if user_question else 'تحلیل کامل پیش‌بینی ۷ روزه ارائه دهید.'}
        
        دستورالعمل‌ها:
        1. پاسخ را به صورت JSON با کلیدهای 'text' و 'visual' برگردان
        2. text: تحلیل کامل با توضیحات دقیق
        3. visual: لیستی از جدول‌ها و نمودارها (هر تعداد که کاربر خواسته باشد)
        
        ساختار visual (لیستی از آیتم‌ها):
        - جدول: {{"type": "table", "headers": ["ستون1", "ستون2", ...], "rows": [["مقدار1", "مقدار2", ...], ...]}}
        - نمودار خطی: {{"type": "chart", "chartType": "line", "title": "عنوان", "labels": ["برچسب1", ...], "datasets": [{{"label": "نام", "data": [مقدار1, ...], "borderColor": "رنگ"}}]}}
        - نمودار میله‌ای: {{"type": "chart", "chartType": "bar", "title": "عنوان", "labels": ["برچسب1", ...], "datasets": [{{"label": "نام", "data": [مقدار1, ...], "backgroundColor": "رنگ"}}]}}
        
        فقط JSON معتبر برگردان.
        """
        
        ai_response = chat_with_ai([
            {'role': 'system', 'content': 'متخصص هواشناسی و پیش‌بینی آب و هوا. پاسخ را به صورت JSON با کلیدهای "text" و "visual" برگردانید. visual باید لیستی از جدول‌ها و نمودارها باشد.'},
            {'role': 'user', 'content': prompt}
        ])
        
        if 'error' in ai_response:
            return jsonify({'error': ai_response['error']}), 500
        
        content = ai_response['choices'][0]['message']['content']
        text, visual = parse_ai_response(content)
        
        if not isinstance(visual, list):
            visual = []
        
        return jsonify({
            'text': text or 'تحلیل انجام شد',
            'visual': visual
        })
        
    except Exception as e:
        print(f"❌ خطا: {e}")
        traceback.print_exc()
        return jsonify({'error': str(e)}), 500

@app.route('/api/chat', methods=['POST'])
@login_required
@premium_required
def api_chat():
    try:
        data = request.get_json()
        if not data:
            return jsonify({'error': 'درخواست نامعتبر'}), 400
        
        user_message = data.get('message', '').strip()
        if not user_message:
            return jsonify({'error': 'پیام خالی است'}), 400
        
        uploaded_files = session.get('uploaded_files', [])
        
        file_content = ""
        if uploaded_files:
            file_content = "\n\n📁 محتوای فایل‌های آپلود شده:\n"
            file_content += "=" * 60 + "\n"
            for f in uploaded_files:
                file_content += f"\n📄 فایل: {f['filename']}\n"
                file_content += "-" * 40 + "\n"
                text = f['text'][:30000]
                file_content += f"محتوا:\n{text}\n"
                file_content += "-" * 40 + "\n"
            file_content += "=" * 60 + "\n\n"
        else:
            file_content = "⚠️ هیچ فایلی آپلود نشده است. لطفاً ابتدا فایل آپلود کنید."
        
        prompt = f"""
        کاربر پیام داد: {user_message}
        
        {file_content}
        
        دستورالعمل‌ها:
        1. اگر فایل آپلود شده، مانند یک متخصص هواشناسی خبره محتوای آن را تحلیل کن
        2. اگر داده‌های عددی در فایل وجود دارد (دما، بارش، رطوبت، فشار، باد و...)، آنها را با دقت تحلیل کن
        3. اگر مقداری 'NON' یا گمشده وجود دارد، با توجه به مقادیر قبل و بعد پیش‌بینی کن
        4. پاسخ را کامل، دقیق و حرفه‌ای بنویس
        5. اگر کاربر درخواست جدول یا نمودار کرد، حتماً بساز
        6. پاسخ را به صورت JSON با کلیدهای "text" و "visual" برگردان
        7. visual باید لیستی از جدول‌ها و/یا نمودارها باشد
        
        ساختار visual (لیستی از آیتم‌ها):
        - جدول: {{"type": "table", "headers": ["ستون1", "ستون2", ...], "rows": [["مقدار1", "مقدار2", ...], ...]}}
        - نمودار: {{"type": "chart", "chartType": "line|bar", "title": "عنوان", "labels": ["برچسب1", ...], "datasets": [{{"label": "نام", "data": [مقدار1, ...], "borderColor|backgroundColor": "رنگ"}}]}}
        
        فقط JSON معتبر برگردان.
        """
        
        ai_response = chat_with_ai([
            {'role': 'system', 'content': 'دستیار هوشمند و متخصص تحلیل داده‌های هواشناسی با ۲۰ سال تجربه. پاسخ را به صورت JSON با کلیدهای "text" و "visual" برگردانید. visual باید لیستی از جدول‌ها و نمودارها باشد.'},
            {'role': 'user', 'content': prompt}
        ])
        
        if 'error' in ai_response:
            return jsonify({'error': ai_response['error']}), 500
        
        content = ai_response['choices'][0]['message']['content']
        text, visual = parse_ai_response(content)
        
        if not isinstance(visual, list):
            visual = []
        
        return jsonify({
            'text': text or 'پاسخ دریافت شد',
            'visual': visual
        })
        
    except Exception as e:
        print(f"❌ خطا: {e}")
        traceback.print_exc()
        return jsonify({'error': str(e)}), 500

@app.route('/api/quick-consultation', methods=['POST'])
@login_required
@vip_required
def api_quick_consultation():
    try:
        data = request.get_json()
        if not data:
            return jsonify({'error': 'درخواست نامعتبر'}), 400
        
        user_question = data.get('question', '').strip()
        lat = data.get('lat')
        lon = data.get('lon')
        location_name = data.get('location_name', 'مکان انتخاب شده')
        
        if not user_question:
            return jsonify({'error': 'لطفاً سوال خود را وارد کنید'}), 400
        
        weather_data = {}
        forecast_data = {}
        
        if lat and lon:
            try:
                lat = float(lat)
                lon = float(lon)
                
                forecast = get_weather_forecast(lat, lon)
                if 'error' not in forecast:
                    daily = forecast.get('daily', {})
                    times = daily.get('time', [])
                    
                    if times:
                        for i, d in enumerate(times[:7]):
                            t_max = daily.get('temperature_2m_max', [])[i] if i < len(daily.get('temperature_2m_max', [])) else None
                            t_min = daily.get('temperature_2m_min', [])[i] if i < len(daily.get('temperature_2m_min', [])) else None
                            precip = daily.get('precipitation_sum', [])[i] if i < len(daily.get('precipitation_sum', [])) else None
                            wind = daily.get('wind_speed_10m_max', [])[i] if i < len(daily.get('wind_speed_10m_max', [])) else None
                            humid = daily.get('relative_humidity_2m_mean', [])[i] if i < len(daily.get('relative_humidity_2m_mean', [])) else None
                            
                            forecast_data[d] = {
                                'temp_max': t_max,
                                'temp_min': t_min,
                                'precipitation': precip,
                                'wind_speed': wind,
                                'humidity': humid
                            }
            except Exception as e:
                print(f"⚠️ خطا در دریافت داده: {e}")
        
        weather_info = ""
        if forecast_data:
            weather_info = f"\n\n📊 داده‌های پیش‌بینی ۷ روزه برای {location_name}:\n"
            for date, values in forecast_data.items():
                weather_info += f"\n📅 {date}:\n"
                weather_info += f"   🌡️ دما: {values['temp_max']}°C / {values['temp_min']}°C\n"
                weather_info += f"   💧 بارش: {values['precipitation']}mm\n"
                weather_info += f"   💨 باد: {values['wind_speed']}km/h\n"
                weather_info += f"   💧 رطوبت: {values['humidity']}%\n"
        else:
            weather_info = "\n\n⚠️ داده‌های هواشناسی برای این مکان در دسترس نیست. لطفاً مکان را روی نقشه انتخاب کنید."

        prompt = f"""
        شما یک متخصص هواشناسی با بیش از ۲۰ سال تجربه هستید.
        
        📍 مکان: {location_name}
        {weather_info}
        
        ❓ سوال کاربر:
        {user_question}
        
        دستورالعمل‌ها:
        1. پاسخ را به صورت یک متخصص خبره هواشناسی بنویسید
        2. از تمام داده‌های موجود استفاده کنید
        3. اگر داده‌ای وجود ندارد، از تجربه و دانش خود استفاده کنید
        4. پاسخ را کامل، دقیق و حرفه‌ای بنویسید
        5. در صورت نیاز، جدول یا نمودار ارائه دهید
        6. پاسخ را به صورت JSON با کلیدهای "text" و "visual" برگردانید
        7. visual باید لیستی از جدول‌ها و/یا نمودارها باشد
        
        ساختار visual (لیستی از آیتم‌ها):
        - جدول: {{"type": "table", "headers": ["ستون1", "ستون2", ...], "rows": [["مقدار1", "مقدار2", ...], ...]}}
        - نمودار خطی: {{"type": "chart", "chartType": "line", "title": "عنوان", "labels": ["برچسب1", ...], "datasets": [{{"label": "نام", "data": [مقدار1, ...], "borderColor": "رنگ"}}]}}
        - نمودار میله‌ای: {{"type": "chart", "chartType": "bar", "title": "عنوان", "labels": ["برچسب1", ...], "datasets": [{{"label": "نام", "data": [مقدار1, ...], "backgroundColor": "رنگ"}}]}}
        
        فقط JSON معتبر برگردانید.
        """
        
        ai_response = chat_with_ai([
            {'role': 'system', 'content': 'شما یک متخصص ارشد هواشناسی با ۲۰ سال تجربه در پیش‌بینی و تحلیل آب و هوا هستید. پاسخ را به صورت JSON با کلیدهای "text" و "visual" برگردانید.'},
            {'role': 'user', 'content': prompt}
        ])
        
        if 'error' in ai_response:
            return jsonify({'error': ai_response['error']}), 500
        
        content = ai_response['choices'][0]['message']['content']
        text, visual = parse_ai_response(content)
        
        if not isinstance(visual, list):
            visual = []
        
        return jsonify({
            'text': text or 'پاسخ دریافت شد',
            'visual': visual
        })
        
    except Exception as e:
        print(f"❌ خطا: {e}")
        traceback.print_exc()
        return jsonify({'error': str(e)}), 500

# ======================== API شهر من (مشابه Samsung Weather) ========================
@app.route('/api/my-city/weather', methods=['POST'])
@login_required
def api_my_city_weather():
    """دریافت آب و هوای شهر با Open-Meteo - مشابه Samsung Weather"""
    try:
        data = request.get_json()
        if not data:
            return jsonify({'error': 'درخواست نامعتبر'}), 400
        
        city_name = data.get('city', '').strip()
        if not city_name:
            return jsonify({'error': 'نام شهر را وارد کنید'}), 400
        
        weather_data = get_weather_by_city(city_name)
        if 'error' in weather_data:
            return jsonify(weather_data), 404
        
        return jsonify(weather_data)
    except Exception as e:
        print(f"❌ خطا: {e}")
        traceback.print_exc()
        return jsonify({'error': str(e)}), 500

@app.route('/api/my-city/current', methods=['POST'])
@login_required
def api_my_city_current():
    """دریافت آب و هوای فعلی با Open-Meteo Current API"""
    try:
        data = request.get_json()
        if not data:
            return jsonify({'error': 'درخواست نامعتبر'}), 400
        
        lat = data.get('lat')
        lon = data.get('lon')
        
        if not lat or not lon:
            return jsonify({'error': 'مختصات نامعتبر'}), 400
        
        weather_data = get_open_meteo_current(float(lat), float(lon))
        if 'error' in weather_data:
            return jsonify(weather_data), 404
        
        return jsonify(weather_data)
    except Exception as e:
        print(f"❌ خطا: {e}")
        traceback.print_exc()
        return jsonify({'error': str(e)}), 500

@app.route('/api/weather-news')
@login_required
def api_weather_news():
    news = get_weather_news_combined()
    return jsonify({'news': news})

@app.route('/api/detect-location-ip')
@login_required
def api_detect_location_ip():
    try:
        response = requests.get('http://ip-api.com/json/', timeout=5)
        if response.status_code == 200:
            data = response.json()
            if data.get('status') == 'success':
                return jsonify({
                    'city': data.get('city', ''),
                    'country': data.get('country', ''),
                    'lat': data.get('lat'),
                    'lon': data.get('lon')
                })
        return jsonify({'error': 'موقعیت یافت نشد'}), 404
    except Exception as e:
        return jsonify({'error': str(e)}), 500

# ======================== قیمت‌ها و خرید ========================
@app.route('/pricing')
@login_required
def pricing():
    return render_template('pricing.html', user=current_user)

@app.route('/checkout/<plan>')
@login_required
def checkout(plan):
    if plan not in ['premium', 'vip']:
        flash('طرح نامعتبر', 'danger')
        return redirect(url_for('pricing'))
    
    prices = {'premium': 10, 'vip': 50}
    settings = get_settings()
    amount = prices.get(plan, 0)
    order_id = generate_order_id()
    qr_code = generate_qr_code(settings.wallet_address)
    
    return render_template('checkout.html', 
                          plan=plan, 
                          amount=amount, 
                          order_id=order_id,
                          wallet_address=settings.wallet_address,
                          qr_code=qr_code,
                          user=current_user)

@app.route('/confirm-payment', methods=['POST'])
@login_required
def confirm_payment():
    try:
        order_id = request.form.get('order_id')
        txid = request.form.get('txid', '').strip()
        plan = request.form.get('plan')
        
        if not order_id or not txid or not plan:
            flash('اطلاعات ناقص است', 'danger')
            return redirect(url_for('pricing'))
        
        existing = Order.query.filter_by(txid=txid).first()
        if existing:
            flash('این کد تراکنش قبلاً ثبت شده است', 'danger')
            return redirect(url_for('checkout', plan=plan))
        
        settings = get_settings()
        prices = {'premium': 10, 'vip': 50}
        amount = prices.get(plan, 0)
        
        order = Order(
            order_id=order_id,
            user_id=current_user.id,
            amount_usdt=amount,
            wallet_address=settings.wallet_address,
            txid=txid,
            status='pending',
            account_type=plan
        )
        db.session.add(order)
        db.session.commit()
        
        flash('تراکنش با موفقیت ثبت شد. پس از تأیید ادمین، اشتراک شما فعال می‌شود.', 'success')
        return redirect(url_for('pricing'))
        
    except Exception as e:
        flash(f'خطا در ثبت تراکنش: {str(e)}', 'danger')
        return redirect(url_for('pricing'))

# ======================== ایجاد دیتابیس و اجرا ========================
with app.app_context():
    db.create_all()
    
    if not User.query.filter_by(username='admin').first():
        admin = User(
            username='admin', 
            email='admin@example.com', 
            is_active=True, 
            is_admin=True,
            account_type='vip'
        )
        admin.set_password('20092010')
        db.session.add(admin)
        db.session.commit()
        print("✅ ادمین ایجاد شد")
    
    if not Setting.query.first():
        db.session.add(Setting())
        db.session.commit()
        print("✅ تنظیمات ایجاد شد")

if __name__ == '__main__':
    os.makedirs(app.config['UPLOAD_FOLDER'], exist_ok=True)
    app.run(debug=True, host='0.0.0.0', port=5000)
