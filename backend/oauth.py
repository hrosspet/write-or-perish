from flask import current_app
from flask_dance.contrib.twitter import make_twitter_blueprint

def init_twitter_blueprint(app):
    redirect_url = f"{app.config['FRONTEND_URL']}/dashboard"
    
    twitter_bp = make_twitter_blueprint(
        api_key=app.config["TWITTER_API_KEY"],
        api_secret=app.config["TWITTER_API_SECRET"],
        redirect_url = redirect_url
    )
    # Register this blueprint on the app  
    app.register_blueprint(twitter_bp, url_prefix="/auth")
    return twitter_bp