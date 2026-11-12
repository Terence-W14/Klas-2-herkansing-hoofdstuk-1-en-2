import csv
from xml.sax.saxutils import escape as xml_escape
from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, KeepTogether
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib.enums import TA_CENTER
from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
import io
import json
import os
import re
import secrets
from datetime import datetime, timezone
from functools import wraps
from urllib.parse import urlparse
from flask import Flask, render_template, request, redirect, url_for, session, abort, jsonify, Response, flash
from flask_sqlalchemy import SQLAlchemy
from sqlalchemy import UniqueConstraint
from werkzeug.security import check_password_hash
from questions import QUESTIONS, ANSWER_KEY, GRADING_RUBRIC

app = Flask(__name__)
app.config['SECRET_KEY'] = os.environ.get('SECRET_KEY', '')
if not app.config['SECRET_KEY']:
    if os.environ.get('RENDER') or os.environ.get('FLASK_ENV') == 'production':
        raise RuntimeError('SECRET_KEY ontbreekt')
    app.config['SECRET_KEY'] = 'dev-only-change-before-deploy'
url = os.environ.get('DATABASE_URL', 'sqlite:///local.db')
if url.startswith('postgres://'): url = 'postgresql://' + url[len('postgres://'):]
app.config['SQLALCHEMY_DATABASE_URI'] = url
app.config['SQLALCHEMY_ENGINE_OPTIONS'] = {'pool_pre_ping': True}
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
app.config['SESSION_COOKIE_HTTPONLY'] = True
app.config['SESSION_COOKIE_SAMESITE'] = 'Lax'
app.config['SESSION_COOKIE_SECURE'] = bool(os.environ.get('RENDER')) or os.environ.get('COOKIE_SECURE') == '1'
app.config['MAX_CONTENT_LENGTH'] = 150_000
app.config['PERMANENT_SESSION_LIFETIME'] = 60 * 60 * 12
app.config['SESSION_REFRESH_EACH_REQUEST'] = True

db = SQLAlchemy(app)
class Student(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(120), nullable=False)
    klass = db.Column(db.String(60), nullable=False)
    created_at = db.Column(db.DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), nullable=False)
    submitted_at = db.Column(db.DateTime(timezone=True))
    answers = db.relationship('Answer', backref='student', cascade='all,delete-orphan')
class Answer(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    student_id = db.Column(db.Integer, db.ForeignKey('student.id'), nullable=False)
    question_id = db.Column(db.Integer, nullable=False)
    value = db.Column(db.Text, nullable=False, default='{}')
    points = db.Column(db.Float)
    feedback = db.Column(db.Text, default='')
    __table_args__ = (UniqueConstraint('student_id', 'question_id'),)
class ExamLock(db.Model):
    student_id = db.Column(db.Integer, db.ForeignKey('student.id'), primary_key=True)
    locked_at = db.Column(db.DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), nullable=False)
    reason = db.Column(db.String(100), nullable=False, default='tabblad verlaten')

class ExamAccess(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    is_open = db.Column(db.Boolean, nullable=False, default=False)

def exam_is_open():
    access = db.session.get(ExamAccess, 1)
    return bool(access and access.is_open)

with app.app_context(): db.create_all()

@app.before_request
def setup():
    if 'csrf' not in session: session['csrf'] = secrets.token_urlsafe(32)

@app.context_processor
def common():
    return {'csrf': session.get('csrf'), 'teacher': session.get('teacher', False), 'exam_title': 'Herkansing economie H1 & H2 – klas 2'}

def csrf_check():
    token = request.headers.get('X-CSRF-Token') or request.form.get('csrf', '')
    if not token or not secrets.compare_digest(token, session.get('csrf', '')): abort(400, 'Ongeldige sessie. Vernieuw de pagina.')

def teacher_only(fn):
    @wraps(fn)
    def inner(*a, **kw):
        if not session.get('teacher'): return redirect(url_for('teacher_login'))
        return fn(*a, **kw)
    return inner

def student_only(fn):
    @wraps(fn)
    def inner(*a, **kw):
        sid = session.get('student_id')
        if not sid: return redirect(url_for('index'))
        student = db.session.get(Student, sid)
        if not student: session.pop('student_id', None); return redirect(url_for('index'))
        return fn(student, *a, **kw)
    return inner

def clean_answer(q, data):
    if not isinstance(data, dict): raise ValueError('Ongeldige invoer')
    kind = q['kind']
    if kind in ('single', 'multi', 'number_choice'):
        choices = data.get('choices', [])
        if not isinstance(choices, list) or len(choices) > q.get('max_choices', 1) or any(x not in (q['options'] if kind == 'number_choice' else list('ABCD')) for x in choices) or len(set(choices)) != len(choices): raise ValueError('Ongeldige keuze')
        return {'choices': choices}
    if kind in ('choice_set','true_false'):
        vals = data.get('values', [])
        count = len(q.get('choices', q.get('statements', [])))
        if not isinstance(vals,list) or len(vals)!=count: raise ValueError('Ongeldige keuzes')
        if kind == 'choice_set':
            if any(v != '' and (not isinstance(v, str) or not v.isdigit() or int(v) >= len(q['choices'][i])) for i, v in enumerate(vals)):
                raise ValueError('Ongeldige keuzes')
        elif any(v not in ('juist', 'onjuist', '') for v in vals):
            raise ValueError('Ongeldige keuzes')
        return {'values': vals}
    answer = data.get('text', '')
    tables = data.get('tables', [])
    if not isinstance(answer, str) or len(answer)>12000: raise ValueError('Antwoord te lang')
    if not isinstance(tables,list) or len(tables)>8: raise ValueError('Te veel tabellen')
    safe_tables=[]
    for t in tables:
        if not isinstance(t,list) or len(t)>12: raise ValueError('Tabel te groot')
        safe=[]
        for row in t:
            if not isinstance(row,list) or len(row)>8 or any(not isinstance(c,str) or len(c)>120 for c in row): raise ValueError('Tabel ongeldig')
            safe.append(row)
        safe_tables.append(safe)
    annotations=data.get('annotations',[])
    if not isinstance(annotations,list) or len(annotations)>8: raise ValueError('Ongeldige pijlen')
    clean_annotations=[]
    for i, marks in enumerate(annotations):
        if not isinstance(marks,list) or len(marks)>20: raise ValueError('Te veel pijlen')
        clean=[]
        for m in marks:
            if not isinstance(m,dict): raise ValueError('Ongeldige pijl')
            start,end,label,shape=m.get('start'),m.get('end'),m.get('label'),m.get('shape')
            width=len(safe_tables[i][0]) if i<len(safe_tables) and safe_tables[i] else 0
            if type(start)!=int or type(end)!=int or start<0 or end<0 or start>=width or end>=width or start==end or shape not in ('boog','pijl') or not isinstance(label,str) or len(label)>32: raise ValueError('Ongeldige pijl')
            clean.append({'start':start,'end':end,'label':label,'shape':shape})
        clean_annotations.append(clean)
    return {'text':answer, 'tables':safe_tables if kind=='calculation' else [], 'annotations':clean_annotations if kind=='calculation' else []}

ALLOWED_CLASSES = ('2A', '2B', '2C', '2D')
TOTAL_POINTS = sum(q['points'] for q in QUESTIONS)
# Verdiepingsvragen tellen voor kader mee, niet voor basis.
# De oorspronkelijke vraagpunten en de leerlingantwoorden blijven ongewijzigd.
KADER_EXTRA_QUESTION_IDS = frozenset({3, 9, 14, 19, 20, 22})
BASIS_POINTS = sum(q['points'] for q in QUESTIONS if q['id'] not in KADER_EXTRA_QUESTION_IDS)

def grade_from_points(points, maximum):
    return round(1 + 9 * points / maximum, 2)

def grading_status(student):
    answers = {a.question_id: a for a in student.answers}
    pending = sum(1 for q in QUESTIONS if q['id'] not in answers or answers[q['id']].points is None)
    total = sum(answers[q['id']].points or 0 for q in QUESTIONS if q['id'] in answers)
    basis_total = sum(answers[q['id']].points or 0 for q in QUESTIONS
                      if q['id'] not in KADER_EXTRA_QUESTION_IDS and q['id'] in answers)
    complete = bool(student.submitted_at) and pending == 0
    return {'total': total, 'basis_total': basis_total, 'pending': pending, 'complete': complete,
            'basis_grade': grade_from_points(basis_total, BASIS_POINTS) if complete else None,
            'kader_grade': grade_from_points(total, TOTAL_POINTS) if complete else None}

def autoscore(q, data):
    """Alleen vaste antwoordvragen automatisch beoordelen; open vragen blijven handmatig."""
    correct = q.get('correct')
    if correct is None:
        return None
    if q['kind'] == 'single':
        return float(q['points']) if data.get('choices') == correct else 0.0
    if q['kind'] == 'choice_set':
        given = data.get('values', [])
        if len(given) != len(correct) or not all(given):
            return 0.0
        if q['id'] == 15:
            return float(sum(a == b for a, b in zip(given, correct)))
        return float(q['points']) if given == correct else 0.0
    return None

def save_answer(student,q,data):
    a = Answer.query.filter_by(student_id=student.id,question_id=q['id']).first()
    if a is None: a=Answer(student_id=student.id,question_id=q['id']);db.session.add(a)
    a.value=json.dumps(data,ensure_ascii=False)
    a.points=autoscore(q,data)
    return a

@app.get('/health')
def health(): return jsonify(status='ok')
@app.route('/',methods=['GET','POST'])
def index():
    if session.get('student_id'):
        s=db.session.get(Student,session['student_id'])
        if s: return redirect(url_for('finished' if s.submitted_at else 'exam'))
    if request.method=='POST':
        csrf_check()
        first_name=' '.join(request.form.get('first_name','').split())
        last_name=' '.join(request.form.get('last_name','').split())
        name=f'{first_name} {last_name}'.strip()
        klass=' '.join(request.form.get('klass','').split())
        if not (1<=len(first_name)<=60 and 1<=len(last_name)<=60 and len(name)<=120 and klass in ALLOWED_CLASSES): flash('Vul je voornaam en achternaam in en kies een geldige klas.');return redirect(url_for('index'))
        if not exam_is_open():
            flash('De toets is momenteel gesloten. Vraag je docent wanneer je mag beginnen.')
            return redirect(url_for('index'))
        student=Student(name=name,klass=klass)
        db.session.add(student);db.session.commit()
        session.clear();session['csrf']=secrets.token_urlsafe(32);session['student_id']=student.id
        return redirect(url_for('exam'))
    return render_template('start.html')

@app.get('/toets')
@student_only
def exam(student):
    if student.submitted_at: return redirect(url_for('finished'))
    if not exam_is_open(): return render_template('class_closed.html',student=student), 403
    if db.session.get(ExamLock,student.id): return render_template('locked.html',student=student)
    questions=[{k:v for k,v in q.items() if k!='correct'} for q in QUESTIONS]
    answers={str(a.question_id):json.loads(a.value) for a in student.answers}
    return render_template('exam.html',student=student,questions=questions,answers=answers)

@app.post('/api/antwoord/<int:qid>')
@student_only
def answer_api(student,qid):
    csrf_check()
    if not exam_is_open(): abort(403,'Toets is gesloten')
    if student.submitted_at or db.session.get(ExamLock,student.id): abort(403,'Toets is afgesloten')
    q=next((q for q in QUESTIONS if q['id']==qid),None)
    if not q: abort(404)
    try: data=clean_answer(q,request.get_json(silent=True))
    except ValueError as e: return jsonify(error=str(e)),400
    save_answer(student,q,data);db.session.commit()
    return jsonify(ok=True)

@app.post('/api/toets-afsluiten')
@student_only
def lock_exam(student):
    csrf_check()
    if not student.submitted_at and not db.session.get(ExamLock,student.id):
        db.session.add(ExamLock(student_id=student.id))
        db.session.commit()
    return jsonify(locked=True)

@app.get('/api/toets-status')
@student_only
def exam_status(student):
    return jsonify(locked=bool(db.session.get(ExamLock,student.id)) or not exam_is_open(),submitted=bool(student.submitted_at),exam_open=exam_is_open())

@app.post('/inleveren')
@student_only
def submit_exam(student):
    csrf_check()
    if not exam_is_open(): abort(403,'Toets is gesloten')
    if db.session.get(ExamLock,student.id): abort(403,'Toets is afgesloten')
    if not student.submitted_at:
        student.submitted_at=datetime.now(timezone.utc)
        db.session.commit()
    return redirect(url_for('finished'))

@app.get('/klaar')
@student_only
def finished(student):
    if not student.submitted_at: return redirect(url_for('exam'))
    return render_template('finished.html',student=student)

@app.post('/afmelden')
def logout_student():
    csrf_check();session.clear();return redirect(url_for('index'))

@app.route('/docent/login',methods=['GET','POST'])
def teacher_login():
    if request.method=='POST':
        csrf_check()
        password=request.form.get('password','')
        configured=os.environ.get('TEACHER_PASSWORD_HASH','')
        if configured and check_password_hash(configured,password):
            session.clear();session.permanent=True;session['csrf']=secrets.token_urlsafe(32);session['teacher']=True
            return redirect(url_for('dashboard'))
        flash('Wachtwoord onjuist.')
    return render_template('teacher_login.html')

@app.post('/docent/uitloggen')
@teacher_only
def teacher_logout():
    csrf_check();session.clear();return redirect(url_for('teacher_login'))

@app.get('/docent')
@teacher_only
def dashboard():
    students=Student.query.all()
    students.sort(key=lambda student: (student.name.split()[-1].casefold(), ' '.join(student.name.split()[:-1]).casefold(), student.klass.casefold()))
    rows=[]
    for s in students:
        answers={a.question_id:a for a in s.answers}
        status=grading_status(s)
        rows.append(dict(student=s,basis_total=status['basis_total'],scored=status['total'],pending=status['pending'] if s.submitted_at else None,basis_grade=status['basis_grade'],kader_grade=status['kader_grade'],locked=bool(db.session.get(ExamLock,s.id))))
    return render_template('dashboard.html', rows=rows, total_points=TOTAL_POINTS, basis_points=BASIS_POINTS, exam_open=exam_is_open())

@app.post('/docent/toets/toegang')
@teacher_only
def toggle_exam_access():
    csrf_check()
    action = request.form.get('action')
    if action not in ('open', 'close'): abort(400)
    access = db.session.get(ExamAccess, 1)
    if access is None:
        access = ExamAccess(id=1, is_open=False)
        db.session.add(access)
    access.is_open = action == 'open'
    db.session.commit()
    flash('De toets is nu ' + ('OPEN' if access.is_open else 'GESLOTEN') + ' voor alle leerlingen.')
    return redirect(url_for('dashboard'))

@app.route('/docent/leerling/<int:sid>', methods=['GET', 'POST'])
@teacher_only
def review(sid):
    student = db.get_or_404(Student, sid)
    if request.method == 'POST':
        csrf_check()
        if not student.submitted_at and not db.session.get(ExamLock, student.id):
            abort(403)
        # Eerst alle ingevoerde beoordelingen controleren; daarna één database-commit.
        updates = []
        for q in QUESTIONS:
            qid = q['id']
            raw = request.form.get(f'points_{qid}', '').strip()
            if raw == '':
                continue  # Niet ingevuld: bestaande beoordeling niet wijzigen.
            try:
                points = float(raw)
            except ValueError:
                abort(400, f'Ongeldige punten bij vraag {qid}')
            if not 0 <= points <= q['points'] or round(points * 2) != points * 2:
                abort(400, f'Ongeldige punten bij vraag {qid}; gebruik stappen van 0,5')
            feedback = request.form.get(f'feedback_{qid}', '')[:2000]
            updates.append((qid, points, feedback))
        for qid, points, feedback in updates:
            a = Answer.query.filter_by(student_id=sid, question_id=qid).first()
            if a is None:
                a = Answer(student_id=sid, question_id=qid, value='{}')
                db.session.add(a)
            a.points = points
            a.feedback = feedback
        db.session.commit()
        flash(f'{len(updates)} beoordelingen in één keer opgeslagen.')
        return redirect(url_for('review', sid=sid))
    ans = {a.question_id: a for a in student.answers}
    items = [dict(q=q, a=ans.get(q['id']), data=json.loads(ans[q['id']].value) if q['id'] in ans else {}) for q in QUESTIONS]
    return render_template('review.html', student=student, items=items, answer_key=ANSWER_KEY,
                           grading_rubric=GRADING_RUBRIC, grading=grading_status(student),
                           total_points=TOTAL_POINTS, basis_points=BASIS_POINTS,
                           kader_extra_ids=KADER_EXTRA_QUESTION_IDS,
                           locked=bool(db.session.get(ExamLock, student.id)))

@app.post('/docent/automatisch-nakijken')
@teacher_only
def rescore_objective_answers():
    """Vul alleen nog onbeoordeelde, gestructureerde antwoorden aan; handmatige punten blijven staan."""
    csrf_check()
    count = 0
    qmap = {q['id']: q for q in QUESTIONS if 'correct' in q}
    for answer in Answer.query.join(Student).filter(Student.submitted_at.isnot(None)).all():
        q = qmap.get(answer.question_id)
        if q is None or answer.points is not None:
            continue
        try:
            data = json.loads(answer.value)
        except (TypeError, ValueError):
            continue
        if not isinstance(data, dict):
            continue
        if q['kind'] == 'single' and isinstance(data.get('choices'), list):
            answer.points = autoscore(q, data)
        elif q['kind'] == 'choice_set' and isinstance(data.get('values'), list):
            answer.points = autoscore(q, data)
        else:
            continue  # Oude tekstantwoorden worden nooit overschreven of geraden.
        count += 1
    db.session.commit()
    flash(f'{count} nog onbeoordeelde keuze-antwoorden automatisch nagekeken. Bestaande punten blijven behouden.')
    return redirect(url_for('dashboard'))

@app.post('/docent/leerling/<int:sid>/ontgrendelen')
@teacher_only
def unlock(sid):
    csrf_check()
    student=db.get_or_404(Student,sid)
    if student.submitted_at: abort(400,'Ingeleverde toets kan niet worden heropend')
    lock=db.session.get(ExamLock,sid)
    if lock: db.session.delete(lock); db.session.commit()
    flash('Toets ontgrendeld. Leerling kan verder met opgeslagen antwoorden.')
    return redirect(url_for('review',sid=sid))

@app.route('/docent/leerling/<int:sid>/verwijderen', methods=['GET','POST'])
@teacher_only
def delete_student(sid):
    student=db.get_or_404(Student,sid)
    if request.method=='POST':
        csrf_check()
        if request.form.get('confirmation') != 'VERWIJDEREN':
            flash('Typ VERWIJDEREN om te bevestigen.')
            return redirect(url_for('delete_student',sid=sid))
        # Verwijder eerst de toetsvergrendeling om een PostgreSQL foreign-key-fout te voorkomen.
        lock = db.session.get(ExamLock, sid)
        if lock is not None:
            db.session.delete(lock)
            db.session.flush()
        # Bijbehorende antwoorden worden via de Student.answers-relatie verwijderd.
        db.session.delete(student)
        db.session.commit()
        flash('Deze toetsafname is verwijderd.')
        return redirect(url_for('dashboard'))
    return render_template('delete_student.html',student=student)

@app.get('/docent/export.csv')
@teacher_only
def export():
    out=io.StringIO();w=csv.writer(out,delimiter=';')
    w.writerow(['Leerling','Klas','Ingeleverd','Punten kader','Max punten kader','Punten basis','Max punten basis','Basiscijfer','Kadercijfer','Nog na te kijken']+[f'Vraag {q["id"]} antwoord' for q in QUESTIONS])
    for s in sorted(Student.query.all(), key=lambda student: (student.name.split()[-1].casefold(), ' '.join(student.name.split()[:-1]).casefold(), student.klass.casefold())):
        ans={a.question_id:a for a in s.answers}
        status=grading_status(s)
        pending=status['pending'] if s.submitted_at else ''
        w.writerow([s.name,s.klass,s.submitted_at.isoformat() if s.submitted_at else '',status['total'],TOTAL_POINTS,status['basis_total'],BASIS_POINTS,status['basis_grade'] if status['basis_grade'] is not None else '',status['kader_grade'] if status['kader_grade'] is not None else '',pending]+[json.dumps(json.loads(ans[q['id']].value),ensure_ascii=False) if q['id'] in ans else '' for q in QUESTIONS])
    return Response('\ufeff'+out.getvalue(),mimetype='text/csv',headers={'Content-Disposition':'attachment; filename="toetsresultaten.csv"','Cache-Control':'no-store'})



def leesbaar_antwoord(vraag, opgeslagen_waarde):
    """Maak van het opgeslagen JSON-antwoord gewone Nederlandse tekst."""
    try:
        data = json.loads(opgeslagen_waarde)
    except (ValueError, TypeError):
        return str(opgeslagen_waarde or '')
    if not isinstance(data, dict):
        return str(data)
    soort = vraag.get('kind')
    if soort in ('single', 'multi', 'number_choice'):
        keuzes = data.get('choices', [])
        opties = vraag.get('options', [])
        uitkomst = []
        for keuze in keuzes:
            if isinstance(keuze, str) and len(keuze) == 1 and keuze.upper() in 'ABCD':
                index = ord(keuze.upper()) - ord('A')
                if index < len(opties):
                    uitkomst.append(f'{keuze.upper()}: {opties[index]}')
                    continue
            uitkomst.append(str(keuze))
        return ' | '.join(uitkomst) if uitkomst else '(niet ingevuld)'
    if soort in ('choice_set', 'true_false'):
        uitkomst = []
        for i, keuze in enumerate(data.get('values', []), start=1):
            tekst = str(keuze)
            if soort == 'choice_set':
                mogelijkheden = vraag.get('choices', [])
                if i <= len(mogelijkheden) and tekst.isdigit() and int(tekst) < len(mogelijkheden[i-1]):
                    tekst = mogelijkheden[i-1][int(tekst)]
            uitkomst.append(f'{i}: {tekst or "(niet ingevuld)"}')
        return ' | '.join(uitkomst) if uitkomst else '(niet ingevuld)'
    delen = []
    if data.get('text'):
        delen.append(data['text'])
    for nummer, tabel in enumerate(data.get('tables', []), start=1):
        rijen = [' / '.join(str(cel) for cel in rij) for rij in tabel]
        delen.append(f'Tabel {nummer}: ' + ' ; '.join(rijen))
    for nummer, pijlen in enumerate(data.get('annotations', []), start=1):
        for pijl in pijlen:
            delen.append(f'Boog/pijl tabel {nummer}: {pijl.get("start", "?")} naar {pijl.get("end", "?")} ({pijl.get("label", "")})')
    return ' | '.join(delen) if delen else '(niet ingevuld)'



@app.get('/docent/leerling/<int:sid>/pdf')
@teacher_only
def download_student_pdf(sid):
    """Eén leesbare PDF met de antwoorden van één leerling."""
    student = db.get_or_404(Student, sid)
    # Dezelfde databasegegevens als in de docentenomgeving, alleen lezen.
    answers = {a.question_id: a for a in student.answers}
    buffer = io.BytesIO()
    font_path = '/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf'
    bold_path = '/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf'
    if os.path.isfile(font_path) and os.path.isfile(bold_path):
        if 'ToetsSans' not in pdfmetrics.getRegisteredFontNames():
            pdfmetrics.registerFont(TTFont('ToetsSans', font_path))
            pdfmetrics.registerFont(TTFont('ToetsSansBold', bold_path))
        regular, bold = 'ToetsSans', 'ToetsSansBold'
    else:
        regular, bold = 'Helvetica', 'Helvetica-Bold'
    styles = getSampleStyleSheet()
    title_style = ParagraphStyle('ToetsTitel', parent=styles['Title'], fontName=bold,
                                 fontSize=15, leading=20, alignment=TA_CENTER, spaceAfter=14)
    info_style = ParagraphStyle('ToetsInfo', parent=styles['Normal'], fontName=regular,
                                fontSize=11, leading=17, spaceAfter=5)
    question_style = ParagraphStyle('ToetsVraag', parent=styles['Normal'], fontName=bold,
                                    fontSize=11, leading=16, spaceBefore=13, spaceAfter=5)
    answer_style = ParagraphStyle('ToetsAntwoord', parent=styles['Normal'], fontName=regular,
                                  fontSize=10, leading=15, spaceAfter=8, wordWrap='CJK')
    story = [Paragraph('HERKANSING ECONOMIE – HOOFDSTUK 1 EN 2 – VERSIE B', title_style),
             Paragraph('<b>Leerling:</b> ' + xml_escape(student.name), info_style),
             Paragraph('<b>Klas:</b> ' + xml_escape(student.klass), info_style)]
    if student.submitted_at:
        story.append(Paragraph('<b>Ingeleverd:</b> ' + xml_escape(student.submitted_at.strftime('%d-%m-%Y %H:%M')), info_style))
    else:
        story.append(Paragraph('<b>Status:</b> Nog niet ingeleverd', info_style))
    story.append(Spacer(1, 10))
    for question in QUESTIONS:
        answer = answers.get(question['id'])
        value = (leesbaar_antwoord(question, answer.value) if answer
                 else '(geen antwoord opgeslagen)')
        # Escape alle leerlingtekst, zodat opmaaktekens geen PDF-fouten geven.
        answer_text = xml_escape(value).replace('\n', '<br/>')
        story.append(KeepTogether([
            Paragraph('Vraag ' + str(question['id']), question_style),
            Paragraph('Antwoord: ' + answer_text, answer_style),
        ]))
    doc = SimpleDocTemplate(buffer, pagesize=A4, rightMargin=46, leftMargin=46,
                            topMargin=48, bottomMargin=48, title='Gemaakte toets economie')
    doc.build(story)
    pdf = buffer.getvalue()
    # Herkenbare bestandsnaam voor Outlook en OneDrive.
    veilige_klas = re.sub(r'[^A-Za-z0-9_-]+', '_', student.klass).strip('_')
    veilige_naam = re.sub(r'[^A-Za-z0-9_-]+', '_', student.name).strip('_')
    bestandsnaam = f'Toets_{veilige_klas}_{veilige_naam}_{student.id}.pdf'
    return Response(pdf, mimetype='application/pdf', headers={
        'Content-Disposition': f'attachment; filename="{bestandsnaam}"',
        'Content-Length': str(len(pdf)),
        'Cache-Control': 'no-store, private',
        'X-Content-Type-Options': 'nosniff',
    })

@app.get('/docent/backup-leesbaar.csv')
@teacher_only
def download_leesbare_backup():
    """Leesbare export: een rij per leerling en vraag, geschikt voor Excel."""
    out = io.StringIO()
    writer = csv.writer(out, delimiter=';')
    writer.writerow(['Leerling', 'Klas', 'Vraagnummer', 'Vraag', 'Antwoord leerling', 'Ingeleverd', 'Punten', 'Feedback'])
    studenten = sorted(Student.query.all(), key=lambda s: (s.klass, s.name.casefold()))
    for student in studenten:
        antwoorden = {a.question_id: a for a in student.answers}
        for vraag in QUESTIONS:
            antwoord = antwoorden.get(vraag['id'])
            writer.writerow([
                student.name, student.klass, vraag['id'], vraag['prompt'],
                leesbaar_antwoord(vraag, antwoord.value) if antwoord else '(geen antwoord opgeslagen)',
                student.submitted_at.strftime('%d-%m-%Y %H:%M') if student.submitted_at else 'Nee',
                antwoord.points if antwoord and antwoord.points is not None else '',
                antwoord.feedback if antwoord else '',
            ])
    return Response(
        '\ufeff' + out.getvalue(),
        mimetype='text/csv; charset=utf-8',
        headers={
            'Content-Disposition': 'attachment; filename="toetsen-leesbaar.csv"',
            'Cache-Control': 'no-store',
            'X-Content-Type-Options': 'nosniff',
        },
    )

@app.get('/docent/backup.json')
@teacher_only
def download_backup():
    """Volledige handmatige back-up, alleen voor ingelogde docent."""
    def timestamp(value):
        return value.isoformat() if value is not None else None

    students = Student.query.order_by(Student.id).all()
    answers = Answer.query.order_by(Answer.id).all()
    locks = ExamLock.query.order_by(ExamLock.student_id).all()
    access = ExamAccess.query.order_by(ExamAccess.id).all()
    backup = {
        'format': 'hoofdstuk-2-toets-3k-backup-v1',
        'created_at_utc': datetime.now(timezone.utc).isoformat(),
        'students': [
            {'id': s.id, 'name': s.name, 'klass': s.klass,
             'created_at': timestamp(s.created_at), 'submitted_at': timestamp(s.submitted_at)}
            for s in students
        ],
        'answers': [
            {'id': a.id, 'student_id': a.student_id, 'question_id': a.question_id,
             'value': a.value, 'points': a.points, 'feedback': a.feedback}
            for a in answers
        ],
        'exam_locks': [
            {'student_id': lock.student_id, 'locked_at': timestamp(lock.locked_at), 'reason': lock.reason}
            for lock in locks
        ],
        'exam_access': [{'id': a.id, 'is_open': a.is_open} for a in access],
        'questions': QUESTIONS,
    }
    filename = 'toets-3k-backup-' + datetime.now(timezone.utc).strftime('%Y%m%d-%H%M%S') + '.json'
    response = Response(
        json.dumps(backup, ensure_ascii=False, indent=2, default=str),
        mimetype='application/json',
        headers={'Content-Disposition': f'attachment; filename="{filename}"', 'Cache-Control': 'no-store'}
    )
    response.headers['X-Content-Type-Options'] = 'nosniff'
    return response

@app.after_request
def headers(response):
    response.headers['X-Content-Type-Options']='nosniff'
    response.headers['X-Frame-Options']='DENY'
    response.headers['Referrer-Policy']='strict-origin-when-cross-origin'
    response.headers['Cache-Control']='no-store'
    response.headers['Content-Security-Policy']="default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; connect-src 'self'; base-uri 'self'; form-action 'self'; frame-ancestors 'none'"
    return response

if __name__=='__main__': app.run(debug=False)
