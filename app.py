from flask import Flask, render_template, request, redirect, url_for, flash, send_file,session
from flask_bcrypt import Bcrypt
from flask_login import LoginManager, UserMixin, login_user, login_required, logout_user, current_user
import psycopg2
import os
import io
import time
import re
import pandas as pd
from hashlib import md5
from dotenv import load_dotenv
from werkzeug.security import check_password_hash, generate_password_hash
from flask_session import Session
from pdf2image import convert_from_bytes
import google.generativeai as genai
from PIL import Image

# Load environment variables
load_dotenv()
DATABASE_URL = os.getenv("DATABASE_URL")
SECRET_KEY = os.getenv("SECRET_KEY", "your_secret_key")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")

if not GEMINI_API_KEY:
    raise EnvironmentError("GEMINI_API_KEY environment variable is not set")

genai.configure(api_key=GEMINI_API_KEY)

# Initialize Flask app
app = Flask(__name__)
app.secret_key = SECRET_KEY
app.config['SESSION_TYPE'] = 'filesystem'
app.config['SESSION_FILE_DIR'] = './flask_sessions'
Session(app)

UPLOAD_FOLDER = 'static/uploads'
app.config['UPLOAD_FOLDER'] = UPLOAD_FOLDER
REPORT_FILE = "student_scores.csv"

# Ensure the CSV file is properly initialized
if not os.path.exists(REPORT_FILE):
    df = pd.DataFrame(columns=["Name", "Class & Section", "Roll No", "Score"])
    df.to_csv(REPORT_FILE, index=False)

# Initialize Flask extensions
bcrypt = Bcrypt(app)
login_manager = LoginManager(app)
login_manager.login_view = "register_login"

# Database connection
def get_db_connection():
    try:
        conn = psycopg2.connect(DATABASE_URL)
        return conn
    except Exception as e:
        print(f"❌ Database connection failed: {e}")
        return None

# User Model
class User(UserMixin):
    def __init__(self, id, name, email):
        self.id = id
        self.name = name
        self.email = email

@login_manager.user_loader
def load_user(user_id):
    conn = get_db_connection()
    if conn:
        cursor = conn.cursor()
        cursor.execute("SELECT id, name, email FROM users WHERE id = %s", (user_id,))
        user = cursor.fetchone()
        conn.close()
        if user:
            return User(id=user[0], name=user[1], email=user[2])
    return None

# Initialize database
def init_db():
    conn = get_db_connection()
    if conn:
        cursor = conn.cursor()
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS users (
                id SERIAL PRIMARY KEY,
                name VARCHAR(100) NOT NULL,
                email VARCHAR(100) UNIQUE NOT NULL,
                password VARCHAR(255) NOT NULL
            )
        ''')
        conn.commit()
        cursor.close()
        conn.close()

@app.route("/", methods=["GET"])
def home():
    return redirect(url_for("register_login"))

@app.route("/register", methods=["GET", "POST"])
def register_login():
    if request.method == "POST":
        action = request.form["action"]

        if action == "register":
            name = request.form["name"]
            email = request.form["email"]
            password = request.form["password"]
            hashed_password = generate_password_hash(password, method="pbkdf2:sha256")

            conn = get_db_connection()
            if conn:
                cursor = conn.cursor()
                try:
                    cursor.execute("INSERT INTO users (name, email, password) VALUES (%s, %s, %s)", 
                                   (name, email, hashed_password))
                    conn.commit()
                    flash("✅ Registration successful! You can now log in.", "success")
                except Exception as e:
                    print(e)
                    #flash("❌ Email already exists!", "danger")
                    conn.rollback()
                finally:
                    cursor.close()
                    conn.close()

        elif action == "login":
            email = request.form["email"]
            password = request.form["password"]

            conn = get_db_connection()
            if conn:
                cursor = conn.cursor()
                cursor.execute("SELECT id, name, email, password FROM users WHERE email = %s", (email,))
                user = cursor.fetchone()
                cursor.close()
                conn.close()

                if user and check_password_hash(user[3], password):
                    user_obj = User(id=user[0], name=user[1], email=user[2])
                    login_user(user_obj)
                    flash("✅ Login successful!", "success")
                    return redirect(url_for("dashboard"))
                else:
                    flash("❌ Invalid email or password!", "danger")

    return render_template("register.html")

@app.route("/dashboard", methods=["GET", "POST"])
@login_required
def dashboard():
    if request.method == "POST":
        name = request.form["name"]
        class_section = request.form["class_section"]
        roll_no = request.form["roll_no"]
        user_score = request.form["user_score"]
        pdf_file = request.files["pdf_file"]

        if not (name and class_section and roll_no and user_score and pdf_file):
            flash("All fields are required!")
            return redirect(url_for("dashboard"))

        filename = pdf_file.filename
        save_path = os.path.join(app.config["UPLOAD_FOLDER"], filename)
        pdf_file.save(save_path)

        try:
            images = convert_pdf_to_images(save_path)
            scores = []
            for image in images:
                score = evaluate_image(image, user_score)
                scores.append(score)

            final_score = ", ".join(scores)
            save_to_file(name, class_section, roll_no, final_score)

            numerical_score_match = re.search(r'(\d+)(?:/|\s+out\s+of\s+)(\d+)', final_score)
            numerical_score = numerical_score_match.group(0) if numerical_score_match else "Score not found"

            flash(f"Final Score: {numerical_score}")
        except Exception as e:
            flash(f"Error: {str(e)}")

        return redirect(url_for("dashboard"))

    return render_template("index.html", name=current_user.name)

@app.route("/report")
@login_required
def report():
    return send_file(REPORT_FILE, as_attachment=True)

@app.route("/logout")
@login_required
def logout():
    logout_user()
    flash("✅ Logged out successfully!", "success")
    return redirect(url_for("register_login"))

# PDF and AI Processing Functions
def convert_pdf_to_images(pdf_path):
    with open(pdf_path, "rb") as f:
        return convert_from_bytes(f.read())

def generate_image_hash(image):
    return md5(convert_image_to_bytes(image)).hexdigest()

def convert_image_to_bytes(image):
    with io.BytesIO() as buffer:
        image.save(buffer, format="PNG")
        return buffer.getvalue()

def evaluate_image(image, user_score):
    image_hash = generate_image_hash(image)
    
    model = genai.GenerativeModel(model_name="gemini-1.5-flash")
    prompt = f"Extract the text from the image and evaluate it to a score of {user_score}. Give a final score as output."

    try:
        response = model.generate_content([prompt, image])
        if response and hasattr(response, 'text'):
            response_text = response.text
            for line in response_text.split('\n'):
                if 'Score'.lower() in line:
                    return line.strip()
        return "Score not found"
    except Exception as e:
        print(f"Error evaluating image: {e}")
        return "Error evaluating image"

def save_to_file(name, class_section, roll_no, score):
    df = pd.read_csv(REPORT_FILE)
    df = pd.concat([df, pd.DataFrame([{"Name": name, "Class & Section": class_section, "Roll No": roll_no, "Score": score}])], ignore_index=True)
    df.to_csv(REPORT_FILE, index=False)

if __name__ == "__main__":
    init_db()
    app.run(debug=True)
