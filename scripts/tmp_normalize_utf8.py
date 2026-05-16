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
    r'C:\Users\hp\OneDrive\Desktop\quizpathshala_website\static\css\site.css',
    r'C:\Users\hp\OneDrive\Desktop\quizpathshala_website\static\js\site.js',
    r'C:\Users\hp\OneDrive\Desktop\quizpathshala_website\templates\base.html',
    r'C:\Users\hp\OneDrive\Desktop\quizpathshala_website\templates\result.html',
    r'C:\Users\hp\OneDrive\Desktop\quizpathshala_website\templates\dashboard.html',
    r'C:\Users\hp\OneDrive\Desktop\quizpathshala_website\templates\admin_dashboard.html',
]
for file in files:
    path = Path(file)
    text = path.read_text(encoding='utf-8-sig')
    path.write_text(text, encoding='utf-8')
print('utf8-normalized')
