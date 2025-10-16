from flask import Flask, render_template, request, jsonify, session
import pandas as pd
import numpy as np
import json
import joblib
from datetime import datetime, timedelta
import pickle
import os
from googleapiclient.discovery import build
from google.auth.transport.requests import Request
from google_auth_oauthlib.flow import InstalledAppFlow
import warnings

# --- NEW: Import for frequent pattern mining ---
from mlxtend.frequent_patterns import apriori, association_rules
# ----------------------------------------------

warnings.filterwarnings('ignore')

app = Flask(__name__)
app.secret_key = 'your-secret-key-change-in-production'

# Google Fit API Configuration
SCOPES = [
    'https://www.googleapis.com/auth/fitness.sleep.read',
    'https://www.googleapis.com/auth/fitness.activity.read',
    'https://www.googleapis.com/auth/fitness.body.read',
    'https://www.googleapis.com/auth/fitness.nutrition.read'
]

# Load ML models
try:
    kmeans = joblib.load('wellness_clustering_model.pkl')
    rf_classifier = joblib.load('risk_prediction_model.pkl')
    # FIX: Load regressor but only use 4 features for it
    rf_regressor = joblib.load('calorie_prediction_model.pkl')
    scaler = joblib.load('feature_scaler.pkl')
    cluster_mapping = joblib.load('cluster_mapping.pkl')
    models_loaded = True
    print("✅ ML Models loaded successfully.")
except Exception as e:
    models_loaded = False
    print(f"⚠️ ML Models not loaded: {e}")

def get_google_fit_credentials():
    creds = None
    if os.path.exists('token.pkl'):
        with open('token.pkl', 'rb') as token:
            creds = pickle.load(token)
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            try:
                creds.refresh(Request())
            except Exception:
                creds = None
        if not creds:
            if os.path.exists('credentials.json'):
                flow = InstalledAppFlow.from_client_secrets_file('credentials.json', SCOPES)
                creds = flow.run_local_server(port=8080)
                with open('token.pkl', 'wb') as token:
                    pickle.dump(creds, token)
            else:
                return None
    return creds

DATA_SOURCES = {
    "steps": "derived:com.google.step_count.delta:com.google.android.gms:merge_step_deltas",
    "calories": "derived:com.google.calories.expended:com.google.android.gms:merge_calories_expended",
    "active_minutes": "derived:com.google.active_minutes:com.google.android.gms:merge_active_minutes",
    "heart_minutes": "derived:com.google.heart_minutes:com.google.android.gms:merge_heart_minutes",
    "weight": "derived:com.google.weight:com.google.android.gms:merge_weight",
    "height": "derived:com.google.height:com.google.android.gms:merge_height",
    "sleep": "derived:com.google.sleep.segment:com.google.android.gms:merged"
}

def fetch_google_fit_data(days=7):
    """Fetch and process raw data from Google Fit for the specified number of days."""
    print(f"\n=== FETCHING RAW DATA FROM GOOGLE FIT API (LAST {days} DAYS) ===")
    creds = get_google_fit_credentials()
    if not creds:
        print("❌ Failed to get Google Fit credentials")
        return None

    service = build("fitness", "v1", credentials=creds)

    now_utc = datetime.utcnow()
    start_time = now_utc - timedelta(days=days)
    end_nanos = int(now_utc.timestamp() * 1e9)
    start_nanos = int(start_time.timestamp() * 1e9)

    print(f"📅 Fetching data from {start_time.date()} to {now_utc.date()}")

    def fetch_raw_data(data_source_id, metric_name):
        """Fetches raw, point-in-time data for a given source."""
        print(f"  📊 Fetching raw {metric_name}...")
        try:
            dataset = service.users().dataSources().datasets().get(
                userId="me",
                dataSourceId=data_source_id,
                datasetId=f"{start_nanos}-{end_nanos}"
            ).execute()
            points = dataset.get("point", [])
            print(f"    ✅ Got {len(points)} raw data points for {metric_name}")
            return points
        except Exception as e:
            print(f"    ❌ Error fetching raw {metric_name}: {e}")
            return []

    raw_data = {metric: fetch_raw_data(source, metric) for metric, source in DATA_SOURCES.items()}

    print("\n📈 Processing and combining daily data...")

    def process_summed_metric(points, value_key='intVal'):
        """Processes metrics that should be summed daily."""
        if not points: return {}
        data_list = []
        for p in points:
            val = p.get("value", [{}])[0].get(value_key)
            if val is not None:
                data_list.append({
                    "time": datetime.fromtimestamp(int(p["startTimeNanos"]) / 1e9),
                    "value": float(val)
                })
        
        if not data_list: return {}
        df = pd.DataFrame(data_list)
        df['date'] = df['time'].dt.strftime('%Y-%m-%d')
        return df.groupby('date')['value'].sum().round().to_dict()

    def process_averaged_metric(points, value_key='fpVal'):
        """Processes metrics that should be averaged daily."""
        if not points: return {}
        data_list = []
        for p in points:
            val = p.get("value", [{}])[0].get(value_key)
            if val is not None:
                data_list.append({
                    "time": datetime.fromtimestamp(int(p["startTimeNanos"]) / 1e9),
                    "value": float(val)
                })
        
        if not data_list: return {}
        df = pd.DataFrame(data_list)
        df['date'] = df['time'].dt.strftime('%Y-%m-%d')
        return df.groupby('date')['value'].mean().round(2).to_dict()
    
    def process_sleep(points):
        """Processes sleep segments into total daily sleep duration."""
        if not points: return {}
        sleep_stages = [2, 4, 5, 6]
        data_list = []
        for p in points:
            stage = p.get("value", [{}])[0].get("intVal")
            if stage in sleep_stages:
                data_list.append({
                    "start": datetime.fromtimestamp(int(p["startTimeNanos"]) / 1e9),
                    "end": datetime.fromtimestamp(int(p["endTimeNanos"]) / 1e9)
                })
        
        if not data_list: return {}
        df = pd.DataFrame(data_list)
        df['duration_minutes'] = (df['end'] - df['start']).dt.total_seconds() / 60
        df['date'] = df['end'].dt.strftime('%Y-%m-%d')
        return df.groupby('date')['duration_minutes'].sum().round().to_dict()

    daily_steps = process_summed_metric(raw_data["steps"])
    daily_calories = process_summed_metric(raw_data["calories"], 'fpVal')
    daily_active_minutes = process_summed_metric(raw_data["active_minutes"])
    daily_heart_minutes = process_summed_metric(raw_data["heart_minutes"], 'fpVal')
    daily_weight = process_averaged_metric(raw_data["weight"])
    daily_height = process_averaged_metric(raw_data["height"])
    daily_sleep = process_sleep(raw_data["sleep"])

    all_dates = set(daily_steps.keys()) | set(daily_calories.keys()) | set(daily_active_minutes.keys()) | set(daily_sleep.keys())
    if not all_dates:
        print("⚠️ No activity data found.")
        return []

    sorted_dates = sorted(list(all_dates))
    
    combined_data = []
    last_known_weight = next(iter(daily_weight.values()), 65.0)
    last_known_height = next(iter(daily_height.values()), 1.70)

    for date_str in sorted_dates:
        if date_str in daily_weight: last_known_weight = daily_weight[date_str]
        if date_str in daily_height: last_known_height = daily_height[date_str]

        day_data = {
            "date": date_str,
            "steps": int(daily_steps.get(date_str, 0)),
            "calories": int(daily_calories.get(date_str, 0)),
            "active_minutes": int(daily_active_minutes.get(date_str, 0)),
            "heart_minutes": int(daily_heart_minutes.get(date_str, 0)),
            "sleep_minutes": int(daily_sleep.get(date_str, 0)),
            "weight": round(last_known_weight, 1),
            "height": round(last_known_height, 2),
            "bmi": round(last_known_weight / (last_known_height ** 2), 2) if last_known_height > 0 else 0
        }
        combined_data.append(day_data)
        
    print(f"✅ Successfully processed {len(combined_data)} days of Google Fit data.")
    return combined_data

def generate_ml_predictions(fitness_data):
    if not models_loaded or not fitness_data:
        return []
    print("\n🤖 Generating ML predictions...")
    predictions = []
    
    for record in fitness_data:
        try:
            # Get user goals for personalized recommendations
            goals = session.get('user_goals', {
                'steps': 10000,
                'calories': 2500,
                'active_minutes': 60,
                'sleep_hours': 7.5
            })
            
            total_active_minutes = max(1, record.get('active_minutes', 0))
            very_active_minutes = min(total_active_minutes, record.get('steps', 0) // 120)
            step_calorie_ratio = record.get('steps', 0) / max(record.get('calories', 1), 1)
            activity_intensity = very_active_minutes / max(total_active_minutes, 1)

            # FIX: Use 7 features for clustering and classification
            features_full = np.array([[
                record.get('steps', 0), 
                total_active_minutes, 
                very_active_minutes,
                record.get('calories', 1600), 
                record.get('bmi', 24.0),
                step_calorie_ratio, 
                activity_intensity
            ]])
            features_scaled = scaler.transform(features_full)
            
            # Use all 7 features for clustering and classification
            cluster = kmeans.predict(features_scaled)[0]
            wellness_category = cluster_mapping.get(cluster, 'Healthy')
            risk_prob = rf_classifier.predict_proba(features_full)[0][1]
            
            # FIX: Use only first 4 features for calorie prediction (steps, active_minutes, very_active_minutes, calories)
            features_regressor = np.array([[
                record.get('steps', 0), 
                total_active_minutes, 
                very_active_minutes,
                record.get('calories', 1600)
            ]])
            predicted_calories = rf_regressor.predict(features_regressor)[0]
            
            recommendations = generate_personalized_recommendations(record, wellness_category, risk_prob > 0.5, goals)

            predictions.append({
                'date': record['date'],
                'wellness_category': wellness_category,
                'risk_probability': float(risk_prob),
                'is_at_risk': bool(risk_prob > 0.5),
                'predicted_calories': int(predicted_calories),
                'recommendations': recommendations,
                'actual_steps': record.get('steps', 0),
                'actual_calories': record.get('calories', 0),
                'active_minutes': record.get('active_minutes', 0),
                'sleep_minutes': record.get('sleep_minutes', 0),
                'bmi': record.get('bmi', 0)
            })
        except Exception as e:
            print(f"❌ Error during prediction for {record['date']}: {e}")
            continue
    
    print(f"✅ Generated {len(predictions)} ML predictions.")
    return predictions

# --- NEW: Function for Frequent Pattern Mining ---
# In app_with_api.py

def find_wellness_patterns(fitness_data, goals):
    """Analyzes historical data to find frequent patterns and insights."""
    if not fitness_data or len(fitness_data) < 3:
        return []

    print("\n🔍 Finding wellness patterns...")
    df = pd.DataFrame(fitness_data)
    
    df['Met_Step_Goal'] = df['steps'] >= goals.get('steps', 10000)
    df['Met_Calorie_Goal'] = df['calories'] >= goals.get('calories', 2500)
    df['Met_Active_Goal'] = df['active_minutes'] >= goals.get('active_minutes', 60)
    df['Good_Sleep'] = df['sleep_minutes'] >= (goals.get('sleep_hours', 7.5) * 60 * 0.9)
    
    transactions = df[['Met_Step_Goal', 'Met_Calorie_Goal', 'Met_Active_Goal', 'Good_Sleep']]
    
    # --- DEBUG: Print the transaction data ---
    print("--- TRANSACTIONS FOR PATTERN MINING ---")
    print(transactions)
    # -----------------------------------------
    
    try:
        frequent_itemsets = apriori(transactions, min_support=0.2, use_colnames=True) # Using 0.2
        if frequent_itemsets.empty:
            # --- DEBUG: Print if no frequent itemsets are found ---
            print("⚠️ No frequent itemsets found with current support level.")
            return ["Not enough consistent patterns found yet. Keep up your activities!"]

        # --- DEBUG: Print the frequent itemsets that were found ---
        print("\n--- FREQUENT ITEMSETS FOUND ---")
        print(frequent_itemsets)
        # --------------------------------------------------------

        rules = association_rules(frequent_itemsets, metric="lift", min_threshold=1.1)
        if rules.empty:
            # --- DEBUG: Print if no association rules are found ---
            print("⚠️ No strong association rules found with current lift level.")
            return ["Found some frequent activities, but no strong connections between them yet."]

        # --- DEBUG: Print the rules that were found ---
        print("\n--- ASSOCIATION RULES FOUND ---")
        print(rules[['antecedents', 'consequents', 'lift', 'confidence']])
        # ----------------------------------------------
            
        insights = []
        for index, rule in rules.iterrows():
            antecedents = ", ".join(list(rule['antecedents'])).replace('_', ' ')
            consequents = ", ".join(list(rule['consequents'])).replace('_', ' ')
            confidence = rule['confidence']
            
            insight = f"💡 **Pattern Found!** When you achieve your **{antecedents}**, you are **{confidence:.0%} likely** to also achieve your **{consequents}**."
            insights.append(insight)
        
        print(f"✅ Found {len(insights)} wellness patterns.")
        return insights if insights else ["No strong wellness patterns discovered yet. Keep logging your data!"]

    except Exception as e:
        print(f"❌ Error during pattern finding: {e}")
        return ["Could not analyze wellness patterns due to an error."]


def generate_personalized_recommendations(record, wellness_category, is_at_risk, goals):
    """Generate personalized recommendations based on user goals"""
    recs = []
    
    steps = record.get('steps', 0)
    active_mins = record.get('active_minutes', 0)
    sleep_mins = record.get('sleep_minutes', 0)
    calories = record.get('calories', 0)
    
    step_goal = goals.get('steps', 10000)
    active_goal = goals.get('active_minutes', 60)
    sleep_goal = goals.get('sleep_hours', 7.5) * 60
    calorie_goal = goals.get('calories', 2500)
    
    # Steps recommendations
    if steps < step_goal * 0.5:
        recs.append(f"🚶‍♂️ You're at {int(steps/step_goal*100)}% of your step goal. Try to reach {int(step_goal*0.7)} steps today")
    elif steps < step_goal:
        recs.append(f"📈 Almost there! Just {step_goal - steps} more steps to reach your goal")
    elif steps >= step_goal:
        recs.append(f"🎉 Goal achieved! You've completed {int(steps/step_goal*100)}% of your step target")
    
    # Activity recommendations
    if active_mins < active_goal * 0.5:
        recs.append(f"⏰ Try to get {int(active_goal*0.7)}+ active minutes today")
    elif active_mins >= active_goal:
        recs.append(f"💪 Great job! You hit your active minutes goal")
    
    # Sleep recommendations
    if sleep_mins < sleep_goal * 0.8:
        recs.append(f"😴 Try to get at least {sleep_goal/60:.1f} hours of sleep tonight")
    elif sleep_mins >= sleep_goal:
        recs.append(f"🌙 Well rested! You got {sleep_mins/60:.1f} hours of quality sleep")
    
    # Calories recommendations
    if calories < calorie_goal * 0.7:
        recs.append(f"🔥 Increase activity to burn more calories (Goal: {calorie_goal})")
    elif calories >= calorie_goal:
        recs.append(f"🔥 Excellent! You burned {int(calories/calorie_goal*100)}% of your calorie goal")
    
    # Wellness category specific
    if wellness_category == 'At Risk' or is_at_risk:
        recs.append("⚠️ Focus on improving your activity levels this week")
    elif wellness_category == 'High Performance':
        recs.append("🏆 Outstanding performance! Remember to include rest days")
    
    return " | ".join(recs) if recs else "Keep up your current routine!"


@app.route('/')
def index():
    return render_template('dashboard.html')

@app.route('/api/fetch-fitness-data')
def fetch_fitness_data_endpoint():
    print("\n=== API ENDPOINT: /api/fetch-fitness-data ===")
    try:
        days = int(request.args.get('days', 7))
        fitness_data = fetch_google_fit_data(days)
        if not fitness_data:
            return jsonify({'status': 'error', 'message': f'No activity data found in Google Fit for the last {days} days.'})
        
        predictions = generate_ml_predictions(fitness_data)
        
        # --- NEW: Find wellness patterns and save to session ---
        user_goals = session.get('user_goals', {
            'steps': 10000,
            'calories': 2500,
            'active_minutes': 60,
            'sleep_hours': 7.5
        })
        patterns = find_wellness_patterns(fitness_data, user_goals)
        session['wellness_patterns'] = patterns
        # ----------------------------------------------------
        
        session['fitness_data'] = fitness_data
        session['predictions'] = predictions
        
        return jsonify({
            'status': 'success', 
            'message': f'Successfully fetched {len(fitness_data)} days from Google Fit.',
            'data_points': len(fitness_data)
        })
    except Exception as e:
        print(f"❌ Error in API endpoint: {e}")
        return jsonify({'status': 'error', 'message': f'An internal error occurred: {str(e)}'})

@app.route('/api/dashboard-data')
def get_dashboard_data():
    fitness_data = session.get('fitness_data')
    predictions = session.get('predictions')
    # --- NEW: Get wellness patterns from session ---
    patterns = session.get('wellness_patterns', [])
    # ---------------------------------------------

    if not fitness_data:
        return jsonify({'error': 'No fitness data in session. Please fetch data first.'})

    df = pd.DataFrame(fitness_data)
    pred_df = pd.DataFrame(predictions) if predictions else pd.DataFrame()
    
    # Ensure proper data types
    df['steps'] = pd.to_numeric(df['steps'], errors='coerce').fillna(0).astype(int)
    df['calories'] = pd.to_numeric(df['calories'], errors='coerce').fillna(0).astype(int)
    df['active_minutes'] = pd.to_numeric(df['active_minutes'], errors='coerce').fillna(0).astype(int)
    df['sleep_minutes'] = pd.to_numeric(df['sleep_minutes'], errors='coerce').fillna(0).astype(int)
    df['sleep_hours'] = df['sleep_minutes'] / 60
    
    print(f"📊 Creating dashboard data for {len(df)} days.")

    # Summary Stats
    summary_stats = {
        'total_days': len(df),
        'avg_steps': int(df['steps'].mean()) if len(df) > 0 else 0,
        'avg_calories': int(df['calories'].mean()) if len(df) > 0 else 0,
        'avg_sleep': round(df['sleep_hours'].mean(), 1) if len(df) > 0 else 0,
        'wellness_score': 0,
        'total_steps': int(df['steps'].sum()),
        'total_calories': int(df['calories'].sum()),
        'max_steps': int(df['steps'].max()) if len(df) > 0 else 0,
        'min_steps': int(df['steps'].min()) if len(df) > 0 else 0
    }
    
    # Calculate wellness score (percentage of days meeting goals)
    if predictions:
        healthy_days = len([p for p in predictions if not p.get('is_at_risk')])
        summary_stats['wellness_score'] = int((healthy_days / len(predictions)) * 100) if predictions else 0

    # Prepare chart data (send raw data, let frontend create charts)
    chart_data = {
        'dates': df['date'].tolist(),
        'steps': df['steps'].tolist(),
        'calories': df['calories'].tolist(),
        'active_minutes': df['active_minutes'].tolist(),
        'sleep_hours': df['sleep_hours'].tolist(),
        'bmi': df['bmi'].tolist()
    }

    return jsonify({
        'chart_data': chart_data,
        'predictions': predictions,
        'summary': summary_stats,
        'raw_data': fitness_data,
        # --- NEW: Add wellness patterns to the response ---
        'wellness_patterns': patterns
        # ------------------------------------------------
    })

@app.route('/api/set-goals', methods=['POST'])
def set_goals():
    try:
        goals = request.json
        session['user_goals'] = {
            'steps': int(goals.get('steps', 10000)),
            'calories': int(goals.get('calories', 2500)),
            'active_minutes': int(goals.get('active_minutes', 60)),
            'sleep_hours': float(goals.get('sleep_hours', 7.5))
        }
        
        # Regenerate predictions with new goals
        fitness_data = session.get('fitness_data')
        if fitness_data:
            predictions = generate_ml_predictions(fitness_data)
            session['predictions'] = predictions

            # --- NEW: Regenerate patterns with new goals ---
            patterns = find_wellness_patterns(fitness_data, session['user_goals'])
            session['wellness_patterns'] = patterns
            # -----------------------------------------------

        return jsonify({'status': 'success', 'message': 'Goals saved successfully!'})
    except Exception as e:
        return jsonify({'status': 'error', 'message': str(e)})

@app.route('/api/get-goals')
def get_goals():
    goals = session.get('user_goals', {
        'steps': 10000,
        'calories': 2500,
        'active_minutes': 60,
        'sleep_hours': 7.5
    })
    return jsonify(goals)

if __name__ == '__main__':
    print("🚀 Starting AI Fitness Wellness Analytics...")
    app.run(debug=True, port=5000)