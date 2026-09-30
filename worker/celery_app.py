from app.core.config import get_settings
from app.providers.queue import make_celery_app

# The same configuration the API uses to enqueue (providers/queue.py), plus the module holding the tasks.
celery_app = make_celery_app(get_settings(), include=["worker.tasks"])
