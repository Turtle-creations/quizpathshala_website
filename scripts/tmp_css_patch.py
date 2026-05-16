from pathlib import Path
root = Path(r'C:\Users\hp\OneDrive\Desktop\quizpathshala_website')
path = root / 'static/css/site.css'
text = path.read_text(encoding='utf-8')
text = text.replace('.site-header.navbar-collapsed .nav-group {\n    opacity: 0;\n    transform: translate3d(-0.7rem, 0, 0);\n    max-width: 0;\n    pointer-events: none;\n}\n', '.site-header.navbar-collapsed .nav-group {\n    opacity: 0;\n    transform: translate3d(-0.7rem, 0, 0);\n    max-width: 0;\n    pointer-events: none;\n}\n\n.site-header.navbar-locked .brand {\n    cursor: default;\n}\n', 1)
text = text.replace('.question-image {\n    width: 100%;\n    margin: 1rem 0 1.4rem;\n    border-radius: var(--radius-md);\n    border: 1px solid rgba(148, 163, 184, 0.14);\n}\n', '.question-image {\n    width: 100%;\n    margin: 1rem 0 1.4rem;\n    border-radius: var(--radius-md);\n    border: 1px solid rgba(148, 163, 184, 0.14);\n}\n\n.admin-question-image-preview {\n    max-width: 220px;\n}\n\n.report-question-box {\n    margin-top: 1rem;\n    padding-top: 0.25rem;\n}\n\n.report-question-box summary {\n    cursor: pointer;\n    color: var(--accent-dark);\n    font-weight: 600;\n}\n\n.report-question-box form {\n    margin-top: 0.85rem;\n}\n', 1)
path.write_text(text, encoding='utf-8')
print('ok')
