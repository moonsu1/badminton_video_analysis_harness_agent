from .bootstrap import environment
from .config import load_settings


if __name__ == '__main__':
    import os
    os.environ.update(environment(load_settings().initialize()))
    from open_webui.main import app
    from .webui_upload import install_video_upload
    install_video_upload(app)
    from open_webui import serve
    serve(host='127.0.0.1', port=int(os.getenv('BADMINTON_WEBUI_PORT', '8080')))
