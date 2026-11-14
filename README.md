🎓 Online Exam System with AI Proctoring

A full-stack web-based Online Examination System built with Python (Flask) and SQLite, featuring automated AI proctoring using YOLOv3-tiny object detection and facial verification to ensure exam integrity.

✨ Key Features

👨‍💼 Admin Portal

Dashboard & Analytics: Monitor active exams, registered students, and overall exam status in real time.

Student Management: Add individual students (add_students.py), bulk-import student records via CSV (student.csv), edit student profiles, and verify student faces before exams.

Subject & Question Bank Management: Create subjects, add multiple-choice questions, and configure exam schedules.

Live Exam Control: Start, pause, or end active exams (active_exams.json & exam_status.json) and view automated student results.

👨‍🎓 Student Portal

Identity & Face Verification: Students undergo face verification prior to entering the exam room.

AI-Proctored Exam Environment: Uses computer vision (YOLOv3-tiny + face detection models) during the exam session to monitor candidate presence and detect unauthorized objects (such as mobile phones or multiple people).

Automated Submission & Scoring: Real-time timer and automatic result calculation upon exam completion.

🛠️ Tech Stack

Backend: Python 3, Flask

Database: SQLite (college_exam_database.db)

Computer Vision / AI Proctoring: OpenCV, YOLOv3-tiny (yolov3-tiny.cfg, yolov3-tiny.weights, coco.names), Face Recognition Models

Frontend: HTML5, CSS3, JavaScript (Jinja2 Templates)

📁 Project Structure

Online_Exam_system/
├── app.py                     # Main Flask application & routing logic
├── setup_database.py          # Initializes SQLite database tables
├── add_students.py            # Script to register students into the database
├── student.csv                # Sample/bulk student data file
├── college_exam_database.db   # SQLite database storing users, questions, and results
├── active_exams.json          # Tracks currently running exam sessions
├── exam_status.json           # Stores exam state flags
├── requirements.txt           # Python dependencies
├── static/                    # CSS stylesheets, JavaScript, images, and face-detection models
├── templates/                 # HTML templates for Admin and Student interfaces
├── uploads/                   # Directory for uploaded student photos and exam assets
└── yolo_model/                # YOLOv3-tiny configuration, weights, and COCO class labels


🔄 System Workflow

flowchart TD
    A[1. Database & Student Setup] --> B[2. Admin Configures Exam]
    B --> C[3. Admin Activates Exam Session]
    C --> D[4. Student Login & Face Verification]
    D --> E{Face Verified?}
    E -- No --> D
    E -- Yes --> F[5. Live Exam + YOLOv3 AI Proctoring]
    F --> G[6. Exam Submission / Timer Expiry]
    G --> H[7. Automated Evaluation & Admin Results]


Step-by-Step Execution Flow

Database Initialization: Running setup_database.py creates the required tables in college_exam_database.db for admins, students, subjects, questions, and results.

Student Onboarding: Admins register candidates manually through the dashboard or import them via student.csv / add_students.py, capturing reference photos in uploads/.

Exam Configuration: The admin creates subjects, populates the question bank, and activates the exam session (updating active_exams.json and exam_status.json).

Candidate Authentication & Face Verification: Students log in and complete facial verification against their registered profile (admin_verify_face.html / student check-in).

Proctored Examination (exam.html): Once verified, the student enters the timed exam interface. The webcam feed runs continuous checks using the models in yolo_model/ and static/models/ to flag prohibited items or absence.

Submission & Result Generation (exam_ended.html): When the student submits the test or the timer expires, answers are evaluated automatically and recorded in college_exam_database.db for review on admin_results.html.

🚀 Installation & Local Setup

1. Clone the Repository

git clone https://github.com/shreyavekhande/Online_Exam_system.git
cd Online_Exam_system


2. Create a Virtual Environment (Recommended)

python -m venv venv
# On Windows:
venv\Scripts\activate
# On macOS/Linux:
source venv/bin/activate


3. Install Dependencies

pip install -r requirements.txt


4. Initialize the Database (If starting fresh)

python setup_database.py
python add_students.py


5. Run the Application

python app.py


Open your browser and navigate to http://127.0.0.1:5000 to access the portal.
