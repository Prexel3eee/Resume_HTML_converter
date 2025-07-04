from flask import Flask, request, jsonify, send_file, render_template_string, send_from_directory
from flask_cors import CORS
from werkzeug.utils import secure_filename
import os
import json
import uuid
import shutil
import zipfile
from datetime import datetime
import threading
import time
from pathlib import Path
import logging

# Import your existing converter class
# Assuming your converter code is in a file called paste.py
from paste import HighQualityHTMLConverter, TextExtractor

app = Flask(__name__)
CORS(app)

# Configuration
app.config['MAX_CONTENT_LENGTH'] = 100 * 1024 * 1024  # 100MB max file size
app.config['UPLOAD_FOLDER'] = 'uploads'
app.config['OUTPUT_FOLDER'] = 'outputs'
app.config['ALLOWED_EXTENSIONS'] = {'pdf', 'doc', 'docx'}

# Create necessary directories
os.makedirs(app.config['UPLOAD_FOLDER'], exist_ok=True)
os.makedirs(app.config['OUTPUT_FOLDER'], exist_ok=True)

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Global storage for conversion jobs
conversion_jobs = {}
job_lock = threading.Lock()


def allowed_file(filename):
    """Check if file extension is allowed"""
    return '.' in filename and \
        filename.rsplit('.', 1)[1].lower() in app.config['ALLOWED_EXTENSIONS']


def get_file_size_mb(file_path):
    """Get file size in MB"""
    return os.path.getsize(file_path) / (1024 * 1024)


class ConversionJob:
    """Class to track conversion job progress"""

    def __init__(self, job_id, files, settings):
        self.job_id = job_id
        self.files = files
        self.settings = settings
        self.status = 'pending'
        self.progress = 0
        self.results = []
        self.error = None
        self.created_at = datetime.now()
        self.completed_at = None

    def to_dict(self):
        return {
            'job_id': self.job_id,
            'status': self.status,
            'progress': self.progress,
            'total_files': len(self.files),
            'completed_files': len([r for r in self.results if r.get('status') == 'success']),
            'failed_files': len([r for r in self.results if r.get('status') == 'failed']),
            'results': self.results,
            'error': self.error,
            'created_at': self.created_at.isoformat(),
            'completed_at': self.completed_at.isoformat() if self.completed_at else None
        }


def process_conversion_job(job):
    """Process conversion job in background thread"""
    try:
        job.status = 'processing'
        job_output_dir = os.path.join(app.config['OUTPUT_FOLDER'], job.job_id)
        os.makedirs(job_output_dir, exist_ok=True)
        converter = HighQualityHTMLConverter(
            output_dir=job_output_dir,
            enable_ocr=job.settings.get('enable_ocr', True),
            image_quality=job.settings.get('image_quality', 85),
            pdf_dpi=job.settings.get('pdf_dpi', 150)
        )
        extract_html = job.settings.get('extract_html', True)
        extract_text = job.settings.get('extract_text', True)
        for i, file_info in enumerate(job.files):
            try:
                file_path = file_info['path']
                filename = file_info['filename']
                logger.info(f"Processing file {i + 1}/{len(job.files)}: {filename}")
                result = {'original_filename': filename, 'status': 'skipped'}
                # Only process if at least one output is requested
                if extract_html or extract_text:
                    if extract_html:
                        html_result = converter.process_document(file_path)
                        result.update(html_result)
                        result['download_url'] = f'/api/download/{job.job_id}/{Path(html_result["output_path"]).name}' if 'output_path' in html_result else None
                        result['preview_url'] = f'/api/preview/{job.job_id}/{Path(html_result["output_path"]).name}' if 'output_path' in html_result else None
                    if extract_text:
                        text_extractor = TextExtractor()
                        text = text_extractor.extract_text(file_path)
                        txt_filename = os.path.splitext(filename)[0] + '.txt'
                        txt_path = os.path.join(job_output_dir, txt_filename)
                        with open(txt_path, 'w', encoding='utf-8') as f:
                            f.write(text)
                        result['text_file'] = txt_filename
                        result['text_status'] = 'success' if text and not text.startswith('Error') else 'failed'
                result['status'] = 'success' if (extract_html and result.get('status') == 'success') or (extract_text and result.get('text_status') == 'success') else 'failed'
                job.results.append(result)
                job.progress = ((i + 1) / len(job.files)) * 100
                logger.info(f"Completed {filename}: {result['status']}")
                # Optionally delete original file if not needed
                if not extract_html and not extract_text:
                    try:
                        os.remove(file_path)
                    except:
                        pass
            except Exception as e:
                logger.error(f"Error processing {file_info['filename']}: {str(e)}")
                job.results.append({
                    'filename': file_info['filename'],
                    'original_filename': file_info['filename'],
                    'status': 'failed',
                    'error': str(e)
                })
                job.progress = ((i + 1) / len(job.files)) * 100
        job.status = 'completed'
        job.completed_at = datetime.now()
        # Clean up uploaded files if not needed
        for file_info in job.files:
            try:
                if not extract_html and not extract_text:
                    os.remove(file_info['path'])
            except:
                pass
    except Exception as e:
        logger.error(f"Job {job.job_id} failed: {str(e)}")
        job.status = 'failed'
        job.error = str(e)
        job.completed_at = datetime.now()


@app.route('/')
def index():
    """Serve the main application page"""
    # Read the HTML from the artifact and serve it
    with open('static/index.html', 'r', encoding='utf-8') as f:
        html_content = f.read()
    return html_content


@app.route('/api/upload', methods=['POST'])
def upload_files():
    """Handle file uploads and start conversion job"""
    try:
        # Check if files are present
        if 'files' not in request.files:
            return jsonify({'error': 'No files provided'}), 400

        files = request.files.getlist('files')
        if not files or all(file.filename == '' for file in files):
            return jsonify({'error': 'No files selected'}), 400

        # Get settings
        settings = {}
        if 'settings' in request.form:
            try:
                settings = json.loads(request.form['settings'])
            except:
                pass

        # Default settings
        settings.setdefault('enable_ocr', True)
        settings.setdefault('image_quality', 85)
        settings.setdefault('pdf_dpi', 150)
        settings.setdefault('max_workers', 4)

        # Validate and save files
        job_id = str(uuid.uuid4())
        job_upload_dir = os.path.join(app.config['UPLOAD_FOLDER'], job_id)
        os.makedirs(job_upload_dir, exist_ok=True)

        file_list = []
        total_size = 0

        for file in files:
            if file and file.filename and allowed_file(file.filename):
                filename = secure_filename(file.filename)
                file_path = os.path.join(job_upload_dir, filename)
                file.save(file_path)

                file_size = os.path.getsize(file_path)
                total_size += file_size

                # Check individual file size (50MB limit)
                if file_size > 50 * 1024 * 1024:
                    os.remove(file_path)
                    return jsonify({'error': f'File {filename} is too large (max 50MB)'}), 400

                file_list.append({
                    'filename': filename,
                    'path': file_path,
                    'size': file_size
                })

        if not file_list:
            shutil.rmtree(job_upload_dir)
            return jsonify({'error': 'No valid files uploaded'}), 400

        # Check total size (100MB limit)
        if total_size > 100 * 1024 * 1024:
            shutil.rmtree(job_upload_dir)
            return jsonify({'error': 'Total file size exceeds 100MB limit'}), 400

        # Create conversion job
        job = ConversionJob(job_id, file_list, settings)

        with job_lock:
            conversion_jobs[job_id] = job

        # Start conversion in background thread
        thread = threading.Thread(target=process_conversion_job, args=(job,))
        thread.daemon = True
        thread.start()

        return jsonify({
            'job_id': job_id,
            'status': 'started',
            'total_files': len(file_list),
            'total_size': total_size
        })

    except Exception as e:
        logger.error(f"Upload error: {str(e)}")
        return jsonify({'error': 'Upload failed'}), 500


@app.route('/api/status/<job_id>', methods=['GET'])
def get_job_status(job_id):
    """Get conversion job status"""
    with job_lock:
        job = conversion_jobs.get(job_id)

    if not job:
        return jsonify({'error': 'Job not found'}), 404

    return jsonify(job.to_dict())


@app.route('/api/download/<job_id>/<filename>', methods=['GET'])
def download_file(job_id, filename):
    """Download converted HTML file"""
    try:
        file_path = os.path.join(app.config['OUTPUT_FOLDER'], job_id, filename)

        if not os.path.exists(file_path):
            return jsonify({'error': 'File not found'}), 404

        return send_file(
            file_path,
            as_attachment=True,
            download_name=filename,
            mimetype='text/html'
        )

    except Exception as e:
        logger.error(f"Download error: {str(e)}")
        return jsonify({'error': 'Download failed'}), 500


@app.route('/api/preview/<job_id>/<filename>', methods=['GET'])
def preview_file(job_id, filename):
    """Preview converted HTML file"""
    try:
        file_path = os.path.join(app.config['OUTPUT_FOLDER'], job_id, filename)

        if not os.path.exists(file_path):
            return jsonify({'error': 'File not found'}), 404

        return send_file(file_path, mimetype='text/html')

    except Exception as e:
        logger.error(f"Preview error: {str(e)}")
        return jsonify({'error': 'Preview failed'}), 500


@app.route('/api/extract_text/<job_id>/<filename>', methods=['GET'])
def extract_text_api(job_id, filename):
    """Extract plain text from a document and return/save it."""
    # Try uploads first, then outputs
    upload_path = os.path.join(app.config['UPLOAD_FOLDER'], job_id, filename)
    output_dir = os.path.join(app.config['OUTPUT_FOLDER'], job_id)
    file_path = upload_path if os.path.exists(upload_path) else os.path.join(output_dir, filename)
    if not os.path.exists(file_path):
        return jsonify({'error': 'File not found'}), 404

    extractor = TextExtractor()
    text = extractor.extract_text(file_path)

    # Save as .txt in output dir
    os.makedirs(output_dir, exist_ok=True)
    txt_filename = os.path.splitext(filename)[0] + '.txt'
    txt_path = os.path.join(output_dir, txt_filename)
    with open(txt_path, 'w', encoding='utf-8') as f:
        f.write(text)

    return jsonify({
        'job_id': job_id,
        'filename': filename,
        'text_file': txt_filename,
        'text': text
    })


@app.route('/api/batch_extract_text/<job_id>', methods=['POST'])
def batch_extract_text_api(job_id):
    """Batch extract plain text for all files in a job."""
    # Find files in uploads or outputs
    upload_dir = os.path.join(app.config['UPLOAD_FOLDER'], job_id)
    output_dir = os.path.join(app.config['OUTPUT_FOLDER'], job_id)
    os.makedirs(output_dir, exist_ok=True)
    files = []
    if os.path.exists(upload_dir):
        files = [f for f in os.listdir(upload_dir) if f.lower().endswith(('.pdf', '.doc', '.docx'))]
    elif os.path.exists(output_dir):
        files = [f for f in os.listdir(output_dir) if f.lower().endswith(('.pdf', '.doc', '.docx'))]
    else:
        return jsonify({'error': 'No files found for job'}), 404

    extractor = TextExtractor()
    results = []
    for filename in files:
        file_path = os.path.join(upload_dir, filename) if os.path.exists(os.path.join(upload_dir, filename)) else os.path.join(output_dir, filename)
        try:
            text = extractor.extract_text(file_path)
            txt_filename = os.path.splitext(filename)[0] + '.txt'
            txt_path = os.path.join(output_dir, txt_filename)
            with open(txt_path, 'w', encoding='utf-8') as f:
                f.write(text)
            results.append({'filename': filename, 'text_file': txt_filename, 'status': 'success'})
        except Exception as e:
            results.append({'filename': filename, 'status': 'failed', 'error': str(e)})
    return jsonify({'job_id': job_id, 'results': results})


@app.route('/api/download-batch/<job_id>', methods=['GET'])
def download_batch(job_id):
    """Download all converted files as a ZIP archive"""
    try:
        with job_lock:
            job = conversion_jobs.get(job_id)

        if not job:
            return jsonify({'error': 'Job not found'}), 404

        if job.status != 'completed':
            return jsonify({'error': 'Job not completed'}), 400

        # Create ZIP file
        job_output_dir = os.path.join(app.config['OUTPUT_FOLDER'], job_id)
        zip_path = os.path.join(job_output_dir, f'{job_id}_converted_files.zip')

        with zipfile.ZipFile(zip_path, 'w', zipfile.ZIP_DEFLATED) as zipf:
            for result in job.results:
                if result.get('status') == 'success' and 'output_path' in result:
                    file_path = result['output_path']
                    if os.path.exists(file_path):
                        arcname = os.path.basename(file_path)
                        zipf.write(file_path, arcname)

        return send_file(
            zip_path,
            as_attachment=True,
            download_name=f'converted_documents_{job_id}.zip',
            mimetype='application/zip'
        )

    except Exception as e:
        logger.error(f"Batch download error: {str(e)}")
        return jsonify({'error': 'Batch download failed'}), 500


@app.route('/api/cleanup/<job_id>', methods=['DELETE'])
def cleanup_job(job_id):
    """Clean up job files and data"""
    try:
        # Remove from memory
        with job_lock:
            if job_id in conversion_jobs:
                del conversion_jobs[job_id]

        # Remove files
        job_upload_dir = os.path.join(app.config['UPLOAD_FOLDER'], job_id)
        job_output_dir = os.path.join(app.config['OUTPUT_FOLDER'], job_id)

        if os.path.exists(job_upload_dir):
            shutil.rmtree(job_upload_dir)

        if os.path.exists(job_output_dir):
            shutil.rmtree(job_output_dir)

        return jsonify({'status': 'cleaned'})

    except Exception as e:
        logger.error(f"Cleanup error: {str(e)}")
        return jsonify({'error': 'Cleanup failed'}), 500


@app.route('/api/health', methods=['GET'])
def health_check():
    """Health check endpoint"""
    return jsonify({
        'status': 'healthy',
        'timestamp': datetime.now().isoformat(),
        'active_jobs': len(conversion_jobs)
    })


# Cleanup old jobs periodically
def cleanup_old_jobs():
    """Clean up jobs older than 24 hours"""
    while True:
        try:
            time.sleep(3600)  # Run every hour

            current_time = datetime.now()
            jobs_to_remove = []

            with job_lock:
                for job_id, job in conversion_jobs.items():
                    # Remove jobs older than 24 hours
                    if (current_time - job.created_at).total_seconds() > 24 * 3600:
                        jobs_to_remove.append(job_id)

                for job_id in jobs_to_remove:
                    del conversion_jobs[job_id]

                    # Clean up files
                    job_upload_dir = os.path.join(app.config['UPLOAD_FOLDER'], job_id)
                    job_output_dir = os.path.join(app.config['OUTPUT_FOLDER'], job_id)

                    if os.path.exists(job_upload_dir):
                        shutil.rmtree(job_upload_dir)

                    if os.path.exists(job_output_dir):
                        shutil.rmtree(job_output_dir)

            if jobs_to_remove:
                logger.info(f"Cleaned up {len(jobs_to_remove)} old jobs")

        except Exception as e:
            logger.error(f"Cleanup thread error: {str(e)}")


# Start cleanup thread
cleanup_thread = threading.Thread(target=cleanup_old_jobs)
cleanup_thread.daemon = True
cleanup_thread.start()


@app.route('/convert', methods=['POST'])
def convert():
    """Handle file uploads and perform real conversion, returning results immediately."""
    try:
        # Check if files are present
        if 'files' not in request.files:
            return jsonify({'error': 'No files provided'}), 400

        files = request.files.getlist('files')
        if not files or all(file.filename == '' for file in files):
            return jsonify({'error': 'No files selected'}), 400

        # Get settings from form
        def get_bool(val, default=True):
            if val is None:
                return default
            if isinstance(val, bool):
                return val
            return str(val).lower() in ['1', 'true', 'yes', 'on']
        
        image_quality = int(request.form.get('imageQuality', 85))
        pdf_dpi = int(request.form.get('pdfDpi', 150))
        enable_ocr = get_bool(request.form.get('enableOcr', True))
        max_workers = int(request.form.get('maxWorkers', 4))
        extract_html = get_bool(request.form.get('extractHtml', True))
        extract_text = get_bool(request.form.get('extractText', True))

        # Validate and save files
        job_id = str(uuid.uuid4())
        job_upload_dir = os.path.join(app.config['UPLOAD_FOLDER'], job_id)
        job_output_dir = os.path.join(app.config['OUTPUT_FOLDER'], job_id)
        os.makedirs(job_upload_dir, exist_ok=True)
        os.makedirs(job_output_dir, exist_ok=True)

        file_list = []
        total_size = 0
        for file in files:
            if file and file.filename and allowed_file(file.filename):
                filename = secure_filename(file.filename)
                file_path = os.path.join(job_upload_dir, filename)
                file.save(file_path)
                file_size = os.path.getsize(file_path)
                total_size += file_size
                # Check individual file size (50MB limit)
                if file_size > 50 * 1024 * 1024:
                    os.remove(file_path)
                    return jsonify({'error': f'File {filename} is too large (max 50MB)'}), 400
                file_list.append({
                    'filename': filename,
                    'path': file_path,
                    'size': file_size
                })
        if not file_list:
            shutil.rmtree(job_upload_dir)
            return jsonify({'error': 'No valid files uploaded'}), 400
        if total_size > 100 * 1024 * 1024:
            shutil.rmtree(job_upload_dir)
            return jsonify({'error': 'Total file size exceeds 100MB limit'}), 400

        # Perform conversion
        converter = HighQualityHTMLConverter(
            output_dir=job_output_dir,
            enable_ocr=enable_ocr,
            image_quality=image_quality,
            pdf_dpi=pdf_dpi
        )
        results = []
        success = 0
        failed = 0
        for file_info in file_list:
            file_path = file_info['path']
            filename = file_info['filename']
            result = {'name': filename}
            try:
                if extract_html:
                    html_result = converter.process_document(file_path)
                    if html_result.get('status') == 'success':
                        result['downloadUrl'] = f'/outputs/{job_id}/{os.path.basename(html_result["output_path"])}'
                        success += 1
                    else:
                        result['error'] = html_result.get('error', 'HTML conversion failed')
                        failed += 1
                if extract_text:
                    text_extractor = TextExtractor()
                    text = text_extractor.extract_text(file_path)
                    txt_filename = os.path.splitext(filename)[0] + '.txt'
                    txt_path = os.path.join(job_output_dir, txt_filename)
                    with open(txt_path, 'w', encoding='utf-8') as f:
                        f.write(text)
                    result['textUrl'] = f'/outputs/{job_id}/{txt_filename}'
                result['status'] = 'success' if 'downloadUrl' in result or 'textUrl' in result else 'failed'
            except Exception as e:
                result['status'] = 'failed'
                result['error'] = str(e)
                failed += 1
            results.append(result)

        return jsonify({
            'success': success,
            'failed': failed,
            'results': results,
            'job_id': job_id
        })
    except Exception as e:
        logger.error(f"/convert error: {str(e)}")
        return jsonify({'error': 'Conversion failed', 'details': str(e)}), 500


@app.route('/outputs/<job_id>/<filename>')
def serve_output_file(job_id, filename):
    """Serve files from the outputs directory (HTML, TXT, etc.)"""
    output_dir = os.path.join('outputs', job_id)
    return send_from_directory(output_dir, filename)


if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000, debug=True)