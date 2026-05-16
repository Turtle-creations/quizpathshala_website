from pathlib import Path
files = [
    r'C:\Users\hp\OneDrive\Desktop\quizpathshala_website\db\database.py',
    r'C:\Users\hp\OneDrive\Desktop\quizpathshala_website\routes\admin.py',
    r'C:\Users\hp\OneDrive\Desktop\quizpathshala_website\routes\quiz.py',
    r'C:\Users\hp\OneDrive\Desktop\quizpathshala_website\services\web_admin_service.py',
    r'C:\Users\hp\OneDrive\Desktop\quizpathshala_website\services\web_identity_service.py',
    r'C:\Users\hp\OneDrive\Desktop\quizpathshala_website\services\web_quiz_service.py',
    r'C:\Users\hp\OneDrive\Desktop\quizpathshala_website\utils\question_image_upload.py',
    r'C:\Users\hp\OneDrive\Desktop\quizpathshala_website\utils\timezone_utils.py',
    r'C:\Users\hp\OneDrive\Desktop\quizpathshala_website\webhook_server.py',
]
for file in files:
    source = Path(file).read_text(encoding='utf-8')
    compile(source, file, 'exec')
print('python-syntax-ok')
