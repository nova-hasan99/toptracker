toptracker/README.md ফাইলটা তৈরি করো:

# 🕒 TopTracker (Clone)

A simple time tracking & screenshot capture tool with desktop client and web dashboard — inspired by **TopTracker**.

## 📂 Project Structure



toptracker/
├── backend/ # Flask web + API server
│ ├── app.py
│ ├── requirements.txt
│ ├── tracker.db (ignored by git)
│ └── uploads/ (ignored by git, stores screenshots)
│
└── desktop/ # PyQt6 desktop client
├── main.py
├── requirements.txt
├── sounds/ (optional, put .wav notification sounds)
├── build/ (ignored)
└── dist/ (ignored)


---

## 🚀 Getting Started

### 1. Clone Repo
```bash
git clone https://github.com/yourname/toptracker.git
cd toptracker

2. Backend Setup (Flask API + Web Dashboard)
cd backend
python -m venv .venv
.\.venv\Scripts\activate   # (Windows PowerShell)
# অথবা Linux/macOS:
# source .venv/bin/activate

pip install -r requirements.txt
python app.py


Runs on http://127.0.0.1:8000

Default SQLite DB → tracker.db

Screenshots save → uploads/

3. Desktop Setup (PyQt6 App)
cd desktop
python -m venv .venv
.\.venv\Scripts\activate   # (Windows PowerShell)

pip install -r requirements.txt
python main.py


Runs a local desktop tracker with:

Login / Register

Toggle ON/OFF time tracking

Captures screenshot every 10 minutes

Shows accept/reject popup with auto-save after 10s

Plays notification sounds from sounds/ (put .wav files)

4. Build Executable (Optional)

To make .exe (Windows):

cd desktop
pyinstaller --onefile --noconsole main.py


The executable will be inside dist/.




Requirements

Python 3.11 (recommended)

SQLite (default, bundled)

Dependencies:

Flask 2.3.x

Flask-JWT-Extended

SQLAlchemy

PyQt6

pynput, mss, requests