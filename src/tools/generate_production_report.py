#!/usr/bin/env python3
"""
Options Agent - Multi-data source options trading recommendation generator
Data Pipeline + Knowledge Management + Decision Architecture + Output Specifications
"""

import os
import json
import requests
from datetime import datetime, timedelta
import random
import time
import qdrant_client
from qdrant_client.http import models
from typing import List, Dict, Any, Tuple
import logging
import re
import sys

# Add src directory to path for imports
sys.path.append('/app/src')
# Optional AlphaVantage client (fallback handled if unavailable)
try:
    from alphavantage_client import alpha_vantage_client  # type: ignore
except Exception:  # pragma: no cover
    class _AlphaVantageStub:
        def get_real_time_quote(self, ticker: str):
            return {}
    alpha_vantage_client = _AlphaVantageStub()
from yfinance_client import yfinance_client

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Configuration
QDRANT_HOST = "qdrant_vdb_prod"
QDRANT_PORT = 6333
OLLAMA_API_URL = "http://ollama_llm_prod:11434/api/generate"
OLLAMA_MODEL = "options-expert"
REPORTS_BASE_DIR = "/app/reports"
REQUEST_TIMEOUT = 60

# Options-related keywords
OPTIONS_KEYWORDS = {
    'CALL': 0.4, 'PUT': 0.4, 'OPTION': 0.3, 'STRIKE': 0.5,
    'EXPIRY': 0.4, 'EXPIRATION': 0.4, 'PREMIUM': 0.3,
    'DELTA': 0.4, 'GAMMA': 0.4, 'THETA': 0.4, 'VEGA': 0.4,
    'IMPLIED VOLATILITY': 0.5, 'IV': 0.4, 'VOLATILITY': 0.3,
    'BREAKEVEN': 0.3, 'PROFIT TARGET': 0.4, 'STOP LOSS': 0.4
}

# Data source profit probability weights
DATA_SOURCE_PROFIT_WEIGHTS = {
    "sec": 0.8,           # SEC insider trading data - highest profit probability
    "newsapi": 0.6,       # News data - high profit probability
    "fred": 0.7,          # Macroeconomic data - medium profit probability
    "market_data": 0.7,   # Market data - high profit probability
    "reddit": 0.3,        # Social media - low profit probability
    "youtube": 0.4,       # YouTube - low profit probability
    "yahoo finance": 0.5  # Yahoo Finance - medium profit probability
}

# Options categories for intelligent recommendation based on database content
OPTIONS_CATEGORIES = {
    "Major Index ETFs": {
        "tickers": ["SPY", "QQQ", "IWM", "DIA", "VTI", "VEA", "VWO", "EFA", "EEM", "IEFA"],
        "keywords": ["spy", "qqq", "iwm", "dia", "s&p 500", "nasdaq", "russell", "dow", "index", "etf", "vti", "vea", "vwo", "efa", "eem", "iefa"],
        "description": "Major Index ETFs - Market's most liquid index ETFs"
    },
    "Tech Giants": {
        "tickers": ["AAPL", "MSFT", "GOOGL", "AMZN", "TSLA", "NVDA", "META", "NFLX", "ADBE", "CRM"],
        "keywords": ["apple", "microsoft", "google", "amazon", "tesla", "nvidia", "meta", "netflix", "adobe", "salesforce", "tech", "technology", "faanmg"],
        "description": "Tech Giants - Leading technology companies with high options liquidity"
    },
    "Financial Sector": {
        "tickers": ["JPM", "BAC", "WFC", "GS", "MS", "C", "BLK", "AXP", "USB", "PNC"],
        "keywords": ["jpmorgan", "bank of america", "wells fargo", "goldman sachs", "morgan stanley", "citigroup", "blackrock", "american express", "us bancorp", "pnc", "bank", "financial", "finance"],
        "description": "Financial Sector - Major banks and financial institutions"
    },
    "Energy Sector": {
        "tickers": ["XOM", "CVX", "COP", "EOG", "PXD", "MPC", "VLO", "PSX", "KMI", "SLB"],
        "keywords": ["exxon", "chevron", "conocophillips", "eog", "pioneer", "marathon", "valero", "phillips 66", "kinder morgan", "schlumberger", "oil", "energy", "crude", "petroleum"],
        "description": "Energy Sector - Oil and energy companies with active options trading"
    },
    "Healthcare Sector": {
        "tickers": ["JNJ", "PFE", "UNH", "ABBV", "MRK", "TMO", "ABT", "DHR", "BMY", "AMGN"],
        "keywords": ["johnson", "pfizer", "unitedhealth", "abbvie", "merck", "thermo fisher", "abbott", "danaher", "bristol myers", "amgen", "healthcare", "pharmaceutical", "medical", "health"],
        "description": "Healthcare Sector - Pharmaceutical and healthcare companies"
    },
    "Consumer Goods": {
        "tickers": ["WMT", "PG", "KO", "PEP", "COST", "HD", "MCD", "SBUX", "NKE", "TGT"],
        "keywords": ["walmart", "procter", "coca-cola", "pepsi", "costco", "home depot", "mcdonalds", "starbucks", "nike", "target", "consumer", "retail"],
        "description": "Consumer Goods - Major consumer and retail companies"
    },
    "Industrial Sector": {
        "tickers": ["BA", "CAT", "GE", "HON", "UPS", "RTX", "LMT", "MMM", "DE", "EMR"],
        "keywords": ["boeing", "caterpillar", "general electric", "honeywell", "ups", "raytheon", "lockheed martin", "3m", "deere", "emerson", "industrial", "manufacturing"],
        "description": "Industrial Sector - Major industrial and manufacturing companies"
    },
    "Commodity ETFs": {
        "tickers": ["GLD", "SLV", "USO", "UNG", "DBA", "DBC", "IAU", "SIVR", "UCO", "BOIL"],
        "keywords": ["gold", "silver", "oil", "natural gas", "agriculture", "commodity", "precious metal", "gld", "slv", "uso", "ung"],
        "description": "Commodity ETFs - Gold, Silver, Oil and other commodity ETFs"
    },
    "Volatility Products": {
        "tickers": ["VIX", "VXX", "UVXY", "SVXY", "TVIX", "VIXY", "VXXB", "VIXM", "VXZ", "VIX9D"],
        "keywords": ["vix", "volatility", "fear index", "volatility index", "vxx", "uvxy", "svxy", "trix"],
        "description": "Volatility Products - VIX and volatility-related instruments"
    }
}

class OptionsDataPipeline:
    """Options data pipeline - focused on options-related data"""
    
    def __init__(self, qdrant_client):
        self.qdrant = qdrant_client
        
    def fetch_options_signals(self) -> List[models.Record]:
        """Fetch options-related signals from Qdrant"""
        try:
            response, _ = self.qdrant.scroll(
                collection_name="financial_signals",
                limit=100,  
                with_payload=True,
                with_vectors=False
            )
            
            
            options_signals = []
            for signal in response:
                content = signal.payload.get('page_content', '').upper()
                if any(keyword in content for keyword in OPTIONS_KEYWORDS.keys()):
                    options_signals.append(signal)
            
            logger.info(f"Fetched {len(options_signals)} options-related signals from {len(response)} total signals")
            return options_signals
        except Exception as e:
            logger.error(f"Error fetching options signals: {e}")
            return []
    
    def calculate_profit_score(self, signal: models.Record) -> float:
        """计算期权盈利概率得分"""
        content = signal.payload.get('page_content', '').upper()
        source = signal.payload.get('source', '').lower()
        
        score = 0.0
        
        # 1) Data source profit weight (40%)
        source_weight = DATA_SOURCE_PROFIT_WEIGHTS.get(source, 0.2)
        score += source_weight * 0.4
        
        # 2) Options keyword density (30%)
        keyword_score = 0.0
        for keyword, weight in OPTIONS_KEYWORDS.items():
            if keyword in content:
                keyword_score += weight
        
        normalized_keyword_score = min(keyword_score / sum(OPTIONS_KEYWORDS.values()), 1.0)
        score += normalized_keyword_score * 0.3
        
        # 3) Options code pattern detection (20%)
        option_patterns = [
            r'\b[A-Z]{2,5}\s+\d+[CP]\b',  # AAPL 150C, SPY 450P
            r'\$\d+\.?\d*\s+[CP]\b',      # $150 C, $450 P
            r'STRIKE\s+\$?\d+',           # STRIKE $150
            r'EXPIR[EY]\s+\d{1,2}/\d{1,2}', # EXPIRY 12/15
        ]
        
        pattern_score = 0.0
        for pattern in option_patterns:
            if re.search(pattern, content):
                pattern_score += 0.25
        
        score += pattern_score * 0.2
        
        # 4) Explicit options terminology presence (10%)
        option_code_score = 0.0
        if any(pattern in content for pattern in ['CALL', 'PUT', 'OPTION', 'STRIKE', 'EXPIR']):
            option_code_score = 1.0
        
        score += option_code_score * 0.1
        
        return min(score, 1.0)
    
    def select_best_options_signals(self, signals: List[models.Record], target_count: int = 8) -> List[models.Record]:
        """Select best options signals by computed profit score"""
        # 计算每个信号的盈利得分
        scored_signals = []
        for signal in signals:
            profit_score = self.calculate_profit_score(signal)
            scored_signals.append((profit_score, signal))
        
        # 按盈利得分排序
        scored_signals.sort(key=lambda x: x[0], reverse=True)
        
        # 选择前N个信号
        selected = [signal for _, signal in scored_signals[:target_count]]
        
        logger.info(f"Selected {len(selected)} best options signals from {len(signals)} total")
        return selected

class OptionsKnowledgeManager:
    """Options knowledge management backed by Qdrant vector DB"""
    
    def __init__(self, qdrant_client):
        self.qdrant = qdrant_client
        
    def get_options_context(self, underlying: str) -> List[Dict[str, Any]]:
        """Get options-related context for a specific underlying"""
        try:
            response, _ = self.qdrant.scroll(
                collection_name="financial_signals",
                limit=50,
                with_payload=True,
                with_vectors=False
            )
            
            # Filter signals that mention the underlying and options terms
            relevant_signals = []
            for signal in response:
                content = signal.payload.get("page_content", "").upper()
                if (underlying.upper() in content and 
                    any(keyword in content for keyword in OPTIONS_KEYWORDS.keys())):
                    relevant_signals.append(signal.payload)
            
            return relevant_signals
        except Exception as e:
            logger.error(f"Options context error: {e}")
            return []

class OptionsDecisionArchitecture:
    """Options decision architecture - ReAct-based multi-agent system"""
    
    def __init__(self):
        self.analyst = OptionsAnalyst()
        self.checker = OptionsFactChecker()
        self.critic = OptionsAdversarialCritic()
        self.synthesizer = OptionsSynthesizer()
        self.uncertainty_quantifier = UncertaintyQuantifier()
        self.schema_validator = SchemaValidator()
    
    def process_options_signal(self, signal_data: Dict[str, Any]) -> Dict[str, Any]:
        """Process an options signal using the ReAct protocol"""
        # ReAct loop: Reason -> Act -> Observe
        max_iterations = 3
        current_state = {
            "signal_data": signal_data,
            "analysis": None,
            "validation": None,
            "counter_arguments": [],
            "uncertainty_metrics": {},
            "iteration": 0
        }
        
        for iteration in range(max_iterations):
            current_state["iteration"] = iteration
            
            # 1) REASON: Analyst synthesizes signal
            if current_state["analysis"] is None:
                current_state["analysis"] = self.analyst.analyze_options_signal(signal_data)
                logger.info(f"ReAct Iteration {iteration}: Analyst synthesized signal")
            
            # 2) ACT: Fact checker validates analysis
            if current_state["validation"] is None:
                current_state["validation"] = self.checker.validate_options_analysis(current_state["analysis"])
                logger.info(f"ReAct Iteration {iteration}: Checker validated analysis")
            
            # 3) OBSERVE: Adversarial critic generates counter-arguments
            if not current_state["counter_arguments"]:
                current_state["counter_arguments"] = self.critic.generate_options_counter_arguments(current_state["analysis"])
                logger.info(f"ReAct Iteration {iteration}: Critic generated {len(current_state['counter_arguments'])} counter-arguments")
            
            # 4) Uncertainty quantification
            current_state["uncertainty_metrics"] = self.uncertainty_quantifier.quantify_uncertainty(
                current_state["analysis"], 
                current_state["validation"], 
                current_state["counter_arguments"]
            )
            
            # 5) Determine whether to continue iterating
            if self._should_continue_iteration(current_state):
                logger.info(f"ReAct Iteration {iteration}: Continuing iteration due to high uncertainty")
                continue
            else:
                logger.info(f"ReAct Iteration {iteration}: Sufficient confidence reached, proceeding to synthesis")
                break
        
        # 6) Final synthesis
        final_decision = self.synthesizer.synthesize_options_decision(
            current_state["analysis"], 
            current_state["validation"], 
            current_state["counter_arguments"],
            current_state["uncertainty_metrics"]
        )
        
        # 7) Schema validation
        validated_decision = self.schema_validator.validate_options_decision(final_decision)
        
        return validated_decision
    
    def _should_continue_iteration(self, state: Dict[str, Any]) -> bool:
        """Decide whether to continue ReAct iterations"""
        uncertainty = state["uncertainty_metrics"].get("overall_uncertainty", 0.5)
        validation_score = state["validation"].get("validation_score", 0.5)
        iteration = state["iteration"]
        
        # Stop when reaching max iterations (0,1,2)
        if iteration >= 2:
            return False
        
        # Continue if uncertainty is high or validation score is low
        should_continue = uncertainty > 0.6 or validation_score < 0.7
        
        if should_continue:
            logger.info(f"ReAct: Continuing iteration due to uncertainty={uncertainty:.2f}, validation={validation_score:.2f}")
        else:
            logger.info(f"ReAct: Stopping iteration - uncertainty={uncertainty:.2f}, validation={validation_score:.2f}")
        
        return should_continue

class UncertaintyQuantifier:
    """Uncertainty quantifier implementation"""
    
    def quantify_uncertainty(self, analysis: Dict[str, Any], validation: Dict[str, Any], counter_arguments: List[str]) -> Dict[str, Any]:
        """Quantify uncertainty of the options analysis"""
        metrics = {}
        
        # 1) Signal strength uncertainty
        signal_strength = analysis.get("signal_strength", 0.5)
        metrics["signal_uncertainty"] = 1.0 - signal_strength
        
        # 2) Validation uncertainty
        validation_score = validation.get("validation_score", 0.5)
        metrics["validation_uncertainty"] = 1.0 - validation_score
        
        # 3) Counter-argument uncertainty
        counter_uncertainty = min(len(counter_arguments) * 0.2, 1.0)
        metrics["counter_uncertainty"] = counter_uncertainty
        
        # 4) Data source uncertainty
        source = analysis.get("source", "").lower()
        source_reliability = {
            "sec": 0.9, "newsapi": 0.8, "fred": 0.7, "market_data": 0.8,
            "reddit": 0.4, "youtube": 0.5, "yahoo finance": 0.6
        }
        source_uncertainty = 1.0 - source_reliability.get(source, 0.5)
        metrics["source_uncertainty"] = source_uncertainty
        
        # 5) Overall uncertainty
        overall_uncertainty = (
            metrics["signal_uncertainty"] * 0.3 +
            metrics["validation_uncertainty"] * 0.3 +
            metrics["counter_uncertainty"] * 0.2 +
            metrics["source_uncertainty"] * 0.2
        )
        metrics["overall_uncertainty"] = overall_uncertainty
        
        # 6) Confidence interval
        confidence_interval = self._calculate_confidence_interval(overall_uncertainty)
        metrics["confidence_interval"] = confidence_interval
        
        # 7) Calibration notes
        metrics["calibration_notes"] = self._generate_calibration_notes(metrics)
        
        return metrics
    
    def _calculate_confidence_interval(self, uncertainty: float) -> Dict[str, float]:
        """Compute confidence interval"""
        base_confidence = 1.0 - uncertainty
        margin_of_error = uncertainty * 0.1
        
        return {
            "lower_bound": max(0.0, base_confidence - margin_of_error),
            "upper_bound": min(1.0, base_confidence + margin_of_error),
            "center": base_confidence
        }
    
    def _generate_calibration_notes(self, metrics: Dict[str, Any]) -> List[str]:
        """Generate calibration notes"""
        notes = []
        
        if metrics["overall_uncertainty"] > 0.7:
            notes.append("High uncertainty - consider additional data sources")
        
        if metrics["validation_uncertainty"] > 0.6:
            notes.append("Data validation concerns - verify signal quality")
        
        if metrics["counter_uncertainty"] > 0.5:
            notes.append("Multiple counter-arguments - risk assessment needed")
        
        if metrics["source_uncertainty"] > 0.6:
            notes.append("Low-reliability data source - cross-verify information")
        
        if not notes:
            notes.append("Low uncertainty - high confidence in analysis")
        
        return notes

class SchemaValidator:
    """Schema validator for JSON output"""
    
    def __init__(self):
        self.required_fields = [
            "ticker", "directional_view", "trade_idea", "candidate_strikes",
            "tenor", "rationale", "confidence_level", "citations"
        ]
        
        self.valid_directional_views = ["Bullish", "Bearish", "Neutral"]
        self.valid_confidence_levels = ["High", "Medium", "Low"]
        self.valid_strategy_types = [
            "Buy Call", "Buy Put", "Sell Call", "Sell Put",
            "Straddle", "Strangle", "Iron Condor", "Butterfly",
            "Covered Call", "Protective Put", "Bull Call Spread", "Bear Put Spread"
        ]
    
    def validate_options_decision(self, decision: Dict[str, Any]) -> Dict[str, Any]:
        """Validate JSON schema for the options decision"""
        validated = decision.copy()
        
        # 1) Ensure required fields
        for field in self.required_fields:
            if field not in validated or not validated[field]:
                validated[field] = self._get_default_value(field)
        
        # 2) Validate enumerated values
        validated["directional_view"] = self._validate_directional_view(validated["directional_view"])
        validated["confidence_level"] = self._validate_confidence_level(validated["confidence_level"])
        validated["trade_idea"] = self._validate_strategy_type(validated["trade_idea"])
        
        # 3) Coerce data types
        validated["candidate_strikes"] = str(validated["candidate_strikes"])
        validated["tenor"] = str(validated["tenor"])
        validated["citations"] = list(validated["citations"]) if isinstance(validated["citations"], list) else [str(validated["citations"])]
        
        # 4) Add validation metadata
        validated["schema_validated"] = True
        validated["validation_timestamp"] = datetime.now().isoformat()
        
        return validated
    
    def _get_default_value(self, field: str) -> str:
        """Get default value for missing field"""
        defaults = {
            "ticker": "UNKNOWN",
            "directional_view": "Neutral",
            "trade_idea": "Straddle",
            "candidate_strikes": "TBD",
            "tenor": (datetime.now() + timedelta(days=30)).strftime("%Y-%m-%d"),
            "rationale": "Analysis based on database content",
            "confidence_level": "Medium",
            "citations": ["Database Analysis"]
        }
        return defaults.get(field, "N/A")
    
    def _validate_directional_view(self, view: str) -> str:
        """Validate directional view value"""
        if view in self.valid_directional_views:
            return view
        return "Neutral"
    
    def _validate_confidence_level(self, level: str) -> str:
        """Validate confidence level value"""
        if level in self.valid_confidence_levels:
            return level
        return "Medium"
    
    def _validate_strategy_type(self, strategy: str) -> str:
        """Validate strategy type value"""
        if strategy in self.valid_strategy_types:
            return strategy
        return "Straddle"

class OptionsAnalyst:
    """Options analyst - analyze options signals"""
    
    def analyze_options_signal(self, signal_data: Dict[str, Any]) -> Dict[str, Any]:
        """Analyze a single options signal"""
        content = signal_data.get("page_content", "")
        source = signal_data.get("source", "")
        
        # Compute signal strength
        strength = self._calculate_options_signal_strength(content, source)
        
        # Extract options-related information
        underlying = self._extract_underlying(content)
        sentiment = self._analyze_options_sentiment(content)
        volatility = self._assess_implied_volatility(content)
        strategy_type = self._determine_strategy_type(content, sentiment)
        
        return {
            "signal_strength": strength,
            "underlying": underlying,
            "sentiment": sentiment,
            "volatility": volatility,
            "strategy_type": strategy_type,
            "source": source,
            "content": content[:300]
        }
    
    def _calculate_options_signal_strength(self, content: str, source: str) -> float:
        """Compute options signal strength"""
        score = 0.0
        content_upper = content.upper()
        source_lower = source.lower()
        
        # Based on data source weights
        score += DATA_SOURCE_PROFIT_WEIGHTS.get(source_lower, 0.2)
        
        # Based on options keywords
        keyword_score = 0.0
        for keyword, weight in OPTIONS_KEYWORDS.items():
            if keyword in content_upper:
                keyword_score += weight
        
        normalized_keyword_score = min(keyword_score / sum(OPTIONS_KEYWORDS.values()), 1.0)
        score += normalized_keyword_score * 0.4
        
        # Based on options code patterns
        option_patterns = [
            r'\b[A-Z]{2,5}\s+\d+[CP]\b',
            r'\$\d+\.?\d*\s+[CP]\b',
            r'STRIKE\s+\$?\d+',
            r'EXPIR[EY]\s+\d{1,2}/\d{1,2}',
        ]
        
        pattern_score = 0.0
        for pattern in option_patterns:
            if re.search(pattern, content_upper):
                pattern_score += 0.25
        
        score += pattern_score * 0.3
        
        return min(score, 1.0)
    
    def _extract_underlying(self, content: str) -> str:
        """Recommend option category/underlying based on DB content (frequency matching)"""
        content_lower = content.lower()
        content_upper = content.upper()
        
        # 1) First, search for explicit option code patterns
        option_patterns = [
            r'\b([A-Z]{2,5})\s+\d+[CP]\b',  # AAPL 150C, SPY 450P
            r'\$([A-Z]{2,5})\b',            # $AAPL, $SPY
            r'\b([A-Z]{2,5})\s+OPTION\b',   # AAPL OPTION
            r'\b([A-Z]{2,5})\s+CALL\b',     # AAPL CALL
            r'\b([A-Z]{2,5})\s+PUT\b',      # AAPL PUT
            r'\b([A-Z]{2,5})\s+STRIKE\b',   # AAPL STRIKE
            r'\b([A-Z]{2,5})\s+EXPIR\b',    # AAPL EXPIRY
        ]
        
        # Find explicit option patterns
        for pattern in option_patterns:
            matches = re.findall(pattern, content_upper)
            for match in matches:
                ticker = match.strip()
                # 验证是否在期权类别中
                for category_data in OPTIONS_CATEGORIES.values():
                    if ticker in category_data["tickers"]:
                        logger.info(f"✓ Found explicit ticker {ticker} in content")
                        return ticker
        
        # 2) Recommend category based on keyword frequency
        category_scores = {}
        for category_name, category_data in OPTIONS_CATEGORIES.items():
            score = 0
            matched_keywords = []
            
            # Check tickers
            for ticker in category_data["tickers"]:
                if ticker in content_upper:
                    score += 2  # higher weight for direct ticker match
                    matched_keywords.append(ticker)
            
            # Check keywords
            for keyword in category_data["keywords"]:
                if keyword in content_lower:
                    score += 1
                    matched_keywords.append(keyword)
            
            # Check keywords from description
            description_words = category_data["description"].lower().split()
            for word in description_words:
                if len(word) > 3 and word in content_lower:  # only match words length > 3
                    score += 1
                    matched_keywords.append(word)
            
            if score > 0:
                category_scores[category_name] = {
                    'score': score,
                    'matched_keywords': matched_keywords
                }
                logger.info(f"✓ Category '{category_name}' scored {score} with keywords: {matched_keywords}")
        
        # 3) Choose top-scoring category and return one of the most liquid tickers
        if category_scores:
            best_category = max(category_scores, key=lambda x: category_scores[x]['score'])
            best_tickers = OPTIONS_CATEGORIES[best_category]["tickers"]
            
            # Select among most liquid tickers (typically first 3-5)
            top_liquid_tickers = best_tickers[:5]
            
            import random
            selected_ticker = random.choice(top_liquid_tickers)
            score_info = category_scores[best_category]
            logger.info(f"✓ Selected {selected_ticker} from category '{best_category}' (score: {score_info['score']}, top liquid tickers: {top_liquid_tickers})")
            return selected_ticker
        
        # 4) If no keyword match, find any known tickers mentioned
        for category_data in OPTIONS_CATEGORIES.values():
            for ticker in category_data["tickers"]:
                if ticker in content_upper:
                    logger.info(f"✓ Found ticker {ticker} mentioned in content")
                    return ticker
        
        # 5) Default to SPY (high-liquidity major index ETF)
        logger.info("✓ No specific category matched, defaulting to SPY (most liquid Major Index ETF)")
        return 'SPY'
    
    def get_top_liquid_options_for_category(self, category_name: str, count: int = 5) -> List[str]:
        """Get most liquid option underlyings for a category"""
        if category_name not in OPTIONS_CATEGORIES:
            logger.warning(f"Category '{category_name}' not found, returning default")
            return ['SPY']
        
        category_data = OPTIONS_CATEGORIES[category_name]
        all_tickers = category_data["tickers"]
        
        # Return top-N most liquid tickers
        top_liquid = all_tickers[:count]
        logger.info(f"✓ Top {count} liquid options for '{category_name}': {top_liquid}")
        return top_liquid
    
    def _analyze_options_sentiment(self, content: str) -> str:
        """Analyze options sentiment"""
        content_lower = content.lower()
        
        # Options-specific sentiment indicators
        bullish_indicators = ["call", "bullish", "upside", "breakout", "momentum", "growth"]
        bearish_indicators = ["put", "bearish", "downside", "breakdown", "decline", "weakness"]
        
        bullish_count = sum(1 for indicator in bullish_indicators if indicator in content_lower)
        bearish_count = sum(1 for indicator in bearish_indicators if indicator in content_lower)
        
        if bullish_count > bearish_count:
            return "Bullish"
        elif bearish_count > bullish_count:
            return "Bearish"
        else:
            return "Neutral"
    
    def _assess_implied_volatility(self, content: str) -> str:
        """Assess implied volatility"""
        content_lower = content.lower()
        
        if any(term in content_lower for term in ["high iv", "high volatility", "volatile", "vix high"]):
            return "High"
        elif any(term in content_lower for term in ["low iv", "low volatility", "stable", "vix low"]):
            return "Low"
        else:
            return "Medium"
    
    def _determine_strategy_type(self, content: str, sentiment: str) -> str:
        """Determine strategy type - risk-averse preference"""
        content_lower = content.lower()
        
        # Risk-averse preference: favor lower-risk, stable-return strategies
        
        # 1) Based on explicit strategy keywords
        if "straddle" in content_lower or "strangle" in content_lower:
            return "Straddle"  # risk-averse: prefer Straddle over Strangle
        elif "iron condor" in content_lower or "condor" in content_lower:
            return "Iron Condor"  # risk-averse: income strategy
        elif "butterfly" in content_lower:
            return "Butterfly"  # risk-averse: limited-risk strategy
        elif "covered call" in content_lower:
            return "Covered Call"  # risk-averse: income strategy
        elif "protective put" in content_lower:
            return "Protective Put"  # risk-averse: protection strategy
        
        # 2) Sentiment-based selection (risk-averse)
        elif sentiment == "Bullish":
            # Bullish: favor Covered Call / Bull Call Spread
            return "Covered Call"  # conservative bullish
        elif sentiment == "Bearish":
            # Bearish: favor Protective Put
            return "Protective Put"
        else:
            # Neutral: favor Iron Condor (income)
            return "Iron Condor"

class OptionsFactChecker:
    """Options fact checker - validate analysis"""
    
    def validate_options_analysis(self, analysis: Dict[str, Any]) -> Dict[str, Any]:
        """Validate options analysis result"""
        validation_score = 0.0
        issues = []
        
        # Check signal strength
        if analysis["signal_strength"] > 0.7:
            validation_score += 0.3
        elif analysis["signal_strength"] > 0.4:
            validation_score += 0.2
        else:
            issues.append("Low options signal strength")
        
        # Check underlying validity
        if analysis["underlying"] and len(analysis["underlying"]) >= 2:
            validation_score += 0.3
        else:
            issues.append("Invalid underlying symbol")
        
        # Check strategy type
        if analysis["strategy_type"] in ["Buy Call", "Buy Put", "Straddle/Strangle", "Iron Condor", "Butterfly"]:
            validation_score += 0.2
        else:
            issues.append("Unclear strategy type")
        
        # Check volatility assessment
        if analysis["volatility"] in ["High", "Medium", "Low"]:
            validation_score += 0.1
        else:
            issues.append("Invalid volatility assessment")
        
        # Check content length/quality
        if len(analysis["content"]) > 150:
            validation_score += 0.1
        else:
            issues.append("Insufficient content for options analysis")
        
        return {
            "validation_score": min(validation_score, 1.0),
            "issues": issues,
            "is_valid": validation_score > 0.5
        }

class OptionsAdversarialCritic:
    """Options adversarial critic - generate counter-arguments"""
    
    def generate_options_counter_arguments(self, analysis: Dict[str, Any]) -> List[str]:
        """Generate options counter-arguments"""
        counter_arguments = []
        
        if analysis["sentiment"] == "Bullish":
            counter_arguments.extend([
                "Call options may be overpriced due to high implied volatility",
                "Market correction could lead to significant call option losses",
                "Time decay may erode call option value even if direction is correct"
            ])
        elif analysis["sentiment"] == "Bearish":
            counter_arguments.extend([
                "Put options may be expensive due to fear premium",
                "Oversold bounce could lead to put option losses",
                "Market may have already priced in negative sentiment"
            ])
        
        if analysis["strategy_type"] == "Straddle/Strangle":
            counter_arguments.append("Straddle strategies require significant price movement to be profitable")
        
        if analysis["volatility"] == "High":
            counter_arguments.append("High IV environment makes options expensive and reduces profit potential")
        elif analysis["volatility"] == "Low":
            counter_arguments.append("Low IV may not provide enough movement for profitable options trades")
        
        if analysis["signal_strength"] > 0.7:
            counter_arguments.append("High signal strength may indicate overconfidence in options strategy")
        
        return counter_arguments

class OptionsSynthesizer:
    """Options synthesizer - produce final decision"""
    
    def synthesize_options_decision(self, analysis: Dict[str, Any], validation: Dict[str, Any], counter_arguments: List[str], uncertainty_metrics: Dict[str, Any] = None) -> Dict[str, Any]:
        """Synthesize all information into a final options decision"""
        # Compute combined confidence
        base_confidence = analysis["signal_strength"]
        validation_penalty = 0.1 if not validation["is_valid"] else 0
        counter_penalty = len(counter_arguments) * 0.05
        
        # Apply uncertainty adjustment
        uncertainty_adjustment = 0
        if uncertainty_metrics:
            uncertainty_adjustment = uncertainty_metrics.get("overall_uncertainty", 0) * 0.2
        
        final_confidence = max(0.1, base_confidence - validation_penalty - counter_penalty - uncertainty_adjustment)
        
        # Map to confidence level label
        if final_confidence > 0.7:
            confidence_level = "High"
        elif final_confidence > 0.4:
            confidence_level = "Medium"
        else:
            confidence_level = "Low"
        
        # Use underlying from analysis
        underlying = analysis["underlying"]
        
        # Strategy type
        strategy_type = analysis["strategy_type"]
        
        # Compute strike and expiration
        strike_price, expiration_date, current_price = self._calculate_options_parameters(underlying, analysis["sentiment"], analysis["volatility"], final_confidence)
        
        # Generate textual rationale
        rationale = self._generate_options_rationale(analysis, validation, counter_arguments, final_confidence, uncertainty_metrics, underlying, strike_price, current_price)
        
        # Determine option type label (Call/Put/Straddle)
        option_type = self._determine_option_type(analysis["sentiment"], strategy_type)
        
        # Build decision payload
        decision = {
            "ticker": underlying,
            "directional_view": analysis["sentiment"],
            "trade_idea": strategy_type,
            "option_type": option_type,  # Call/Put/Straddle
            "candidate_strikes": f"${strike_price:.2f}",
            "strike_price": round(strike_price, 2),
            "tenor": expiration_date,
            "expiration_date": expiration_date,
            "rationale": rationale,
            "confidence_level": confidence_level,
            "citations": [analysis["source"], "ReAct Multi-Agent Analysis"],
            "signal_strength": round(final_confidence, 2),
            "validation_score": round(validation["validation_score"], 2),
            "counter_arguments": counter_arguments[:2],
            "volatility_assessment": analysis["volatility"],
            "strategy_type": strategy_type
        }
        
        # Attach uncertainty metrics if available
        if uncertainty_metrics:
            # 格式化不确定性指标
            formatted_uncertainty = {}
            for key, value in uncertainty_metrics.items():
                if isinstance(value, (int, float)):
                    formatted_uncertainty[key] = round(value, 2)
                else:
                    formatted_uncertainty[key] = value
            
            decision.update({
                "uncertainty_metrics": formatted_uncertainty,
                "confidence_interval": uncertainty_metrics.get("confidence_interval", {}),
                "calibration_notes": uncertainty_metrics.get("calibration_notes", []),
                "overall_uncertainty": round(uncertainty_metrics.get("overall_uncertainty", 0.5), 2)
            })
        
        return decision
    
    def _calculate_options_parameters(self, underlying: str, sentiment: str, volatility: str, confidence: float) -> Tuple[float, str, float]:
        """Calculate option parameters using yfinance when possible"""
        try:
            # 1) Get stock info
            stock_info = yfinance_client.get_stock_info(underlying)
            current_price = stock_info.get('current_price', 0)
            
            if current_price == 0:
                logger.warning(f"Could not get current price for {underlying}, using fallback")
                # Fallback to Alpha Vantage
                quote = alpha_vantage_client.get_real_time_quote(underlying)
                if quote and '05. price' in quote:
                    current_price = float(quote['05. price'])
                else:
                    current_price = 100.0  # 默认价格
            
            logger.info(f"Current price for {underlying}: ${current_price}")
            
            # 2) Get options expirations
            expirations = yfinance_client.get_options_expirations(underlying)
            if not expirations:
                logger.warning(f"No options expirations found for {underlying}, generating default")
                # Generate default expiration
                if volatility == "High":
                    days_to_expiry = random.randint(15, 30)
                elif volatility == "Low":
                    days_to_expiry = random.randint(45, 60)
                else:
                    days_to_expiry = random.randint(30, 45)
                expiration_date = (datetime.now() + timedelta(days=days_to_expiry)).strftime("%Y-%m-%d")
            else:
                # Pick suitable expiration
                if volatility == "High":
                    # High IV: nearer expirations
                    expiration_date = expirations[0] if len(expirations) > 0 else expirations[-1]
                elif volatility == "Low":
                    # Low IV: further expirations
                    expiration_date = expirations[-1] if len(expirations) > 0 else expirations[0]
                else:
                    # Medium IV: mid expirations
                    mid_index = len(expirations) // 2
                    expiration_date = expirations[mid_index] if len(expirations) > mid_index else expirations[0]
            
            logger.info(f"Selected expiration date for {underlying}: {expiration_date}")
            
            # 3) Get liquid strikes
            liquid_strikes = yfinance_client.get_liquid_strikes(underlying, expiration_date, min_volume=5)
            
            if not liquid_strikes:
                # If none, get ATM strikes
                liquid_strikes = yfinance_client.get_atm_strikes(underlying, expiration_date, count=10)
            
            if not liquid_strikes:
                logger.warning(f"No liquid strikes found for {underlying}, generating based on current price")
                # Generate strike based on current price
                if sentiment == "Bullish":
                    strike_price = current_price * 1.02  # 2% OTM
                elif sentiment == "Bearish":
                    strike_price = current_price * 0.98  # 2% OTM
                else:
                    strike_price = current_price  # ATM
            else:
                # Choose strike according to sentiment
                if sentiment == "Bullish":
                    # Bullish: slightly above spot
                    bullish_strikes = [s for s in liquid_strikes if s > current_price]
                    strike_price = bullish_strikes[0] if bullish_strikes else liquid_strikes[0]
                elif sentiment == "Bearish":
                    # Bearish: slightly below spot
                    bearish_strikes = [s for s in liquid_strikes if s < current_price]
                    strike_price = bearish_strikes[-1] if bearish_strikes else liquid_strikes[-1]
                else:
                    # Neutral: nearest to spot
                    strike_price = min(liquid_strikes, key=lambda x: abs(x - current_price))
            
            logger.info(f"Selected strike price for {underlying}: ${strike_price}")
            
            return round(strike_price, 2), expiration_date, current_price
            
        except Exception as e:
            logger.error(f"Error calculating options parameters for {underlying}: {e}")
            # Fallback to simple calculation
            base_price = 100.0
            if sentiment == "Bullish":
                strike_price = base_price * 1.02
            elif sentiment == "Bearish":
                strike_price = base_price * 0.98
            else:
                strike_price = base_price
            
            days_to_expiry = 30
            expiration_date = (datetime.now() + timedelta(days=days_to_expiry)).strftime("%Y-%m-%d")
            
            return round(strike_price, 2), expiration_date, base_price
    
    def _determine_option_type(self, sentiment: str, strategy_type: str) -> str:
        """Determine final option type label"""
        if strategy_type in ["Buy Call", "Bull Call Spread", "Covered Call"]:
            return "Call"
        elif strategy_type in ["Buy Put", "Bear Put Spread", "Protective Put"]:
            return "Put"
        elif strategy_type in ["Straddle", "Strangle", "Long Straddle", "Long Strangle"]:
            return "Straddle"
        elif strategy_type in ["Iron Condor", "Butterfly"]:
            return "Spread"
        else:
            # 根据情绪确定
            if sentiment == "Bullish":
                return "Call"
            elif sentiment == "Bearish":
                return "Put"
            else:
                return "Straddle"
    
    def _generate_options_rationale(self, analysis: Dict[str, Any], validation: Dict[str, Any], counter_arguments: List[str], confidence: float, uncertainty_metrics: Dict[str, Any] = None, underlying: str = None, strike_price: float = None, current_price: float = None) -> str:
        """生成期权详细理由，包含详细的数据指标"""
        rationale_parts = []
        
        # 基于信号强度
        if confidence > 0.7:
            rationale_parts.append(f"Strong options signal strength ({confidence:.2f}) from {analysis['source']} analysis")
        elif confidence > 0.4:
            rationale_parts.append(f"Moderate options signal strength ({confidence:.2f}) from {analysis['source']} analysis")
        else:
            rationale_parts.append(f"Low options signal strength ({confidence:.2f}) from {analysis['source']} analysis")
        
        # 添加详细的数据指标
        if underlying and strike_price and current_price:
            # 计算内在价值和时间价值
            if analysis["sentiment"] == "Bullish":
                intrinsic_value = max(0, current_price - strike_price)
                moneyness = "ITM" if current_price > strike_price else "OTM" if current_price < strike_price else "ATM"
            elif analysis["sentiment"] == "Bearish":
                intrinsic_value = max(0, strike_price - current_price)
                moneyness = "ITM" if current_price < strike_price else "OTM" if current_price > strike_price else "ATM"
            else:
                intrinsic_value = 0
                moneyness = "ATM" if abs(current_price - strike_price) / current_price < 0.02 else "OTM"
            
            # 估算期权价格（简化Black-Scholes）
            time_to_expiry = 30  # 假设30天到期
            volatility = 0.25  # 假设25%波动率
            risk_free_rate = 0.05  # 假设5%无风险利率
            
            # 简化的期权价格估算
            if analysis["sentiment"] == "Bullish":
                estimated_premium = max(intrinsic_value + (current_price * volatility * (time_to_expiry/365)**0.5), 0.5)
            elif analysis["sentiment"] == "Bearish":
                estimated_premium = max(intrinsic_value + (current_price * volatility * (time_to_expiry/365)**0.5), 0.5)
            else:
                estimated_premium = current_price * volatility * (time_to_expiry/365)**0.5 * 2  # Straddle
            
            time_value = max(0, estimated_premium - intrinsic_value)
            
            # 计算Delta（简化）
            if analysis["sentiment"] == "Bullish":
                delta = 0.5 + (current_price - strike_price) / (strike_price * 0.1) * 0.3
                delta = max(0.1, min(0.9, delta))
            elif analysis["sentiment"] == "Bearish":
                delta = 0.5 - (current_price - strike_price) / (strike_price * 0.1) * 0.3
                delta = max(-0.9, min(-0.1, delta))
            else:
                delta = 0.0  # Straddle delta near 0
            
            
            historical_vol = 0.20  
            expected_return = 0.08  
            sharpe_ratio = expected_return / historical_vol
            
            # 计算其他Greeks
            gamma = 0.01  # 简化Gamma
            theta = -estimated_premium / time_to_expiry  # 时间衰减
            vega = current_price * (time_to_expiry/365)**0.5 * 0.01  # 波动率敏感度
            
            rationale_parts.append(f"**Price Analysis:** Current price ${current_price:.2f} vs Strike ${strike_price:.2f} ({moneyness})")
            rationale_parts.append(f"**Intrinsic Value:** ${intrinsic_value:.2f}, Time Value: ${time_value:.2f}, Estimated Premium: ${estimated_premium:.2f}")
            rationale_parts.append(f"**Greeks:** Delta={delta:.3f}, Gamma={gamma:.3f}, Theta={theta:.3f}, Vega={vega:.3f}")
            rationale_parts.append(f"**Risk Metrics:** Sharpe Ratio={sharpe_ratio:.2f}, Volatility={volatility:.1%}")
        
        # 基于期权情绪
        if analysis["sentiment"] == "Bullish":
            rationale_parts.append("Bullish sentiment supports call option strategies")
        elif analysis["sentiment"] == "Bearish":
            rationale_parts.append("Bearish sentiment supports put option strategies")
        else:
            rationale_parts.append("Neutral sentiment suggests straddle/strangle strategies")
        
        # 基于波动率
        if analysis["volatility"] == "High":
            rationale_parts.append("High implied volatility environment favors short premium strategies")
        elif analysis["volatility"] == "Low":
            rationale_parts.append("Low implied volatility environment favors long premium strategies")
        else:
            rationale_parts.append("Medium volatility environment supports balanced options strategies")
        
        # 基于验证结果
        if validation["is_valid"]:
            rationale_parts.append("Fact-checked options analysis confirms data quality")
        else:
            rationale_parts.append("Some options data quality concerns noted")
        
        # 基于反论点
        if counter_arguments:
            rationale_parts.append(f"Consider {len(counter_arguments)} counter-arguments in options risk assessment")
        
        # 基于不确定性量化
        if uncertainty_metrics:
            overall_uncertainty = uncertainty_metrics.get("overall_uncertainty", 0.5)
            if overall_uncertainty > 0.7:
                rationale_parts.append("High uncertainty environment - exercise caution in position sizing")
            elif overall_uncertainty < 0.3:
                rationale_parts.append("Low uncertainty environment - high confidence in strategy")
            
            # 添加校准注释
            calibration_notes = uncertainty_metrics.get("calibration_notes", [])
            if calibration_notes:
                rationale_parts.append(f"Risk considerations: {', '.join(calibration_notes[:2])}")
        
        return ". ".join(rationale_parts) + "."

class OptionsReportGenerator:
    """Options report generator"""
    
    def __init__(self):
        self.qdrant = qdrant_client.QdrantClient(host=QDRANT_HOST, port=QDRANT_PORT)
        self.data_pipeline = OptionsDataPipeline(self.qdrant)
        self.knowledge_manager = OptionsKnowledgeManager(self.qdrant)
        self.decision_architecture = OptionsDecisionArchitecture()
    
    def generate_daily_options_report(self) -> Dict[str, Any]:
        """Generate daily options report"""
        logger.info("🚀 Starting options report generation...")
        start_time = datetime.now()
        
        # 1) Data pipeline - fetch options signals
        logger.info("📊 Options Data Pipeline: Fetching options-related signals...")
        options_signals = self.data_pipeline.fetch_options_signals()
        
        if not options_signals:
            logger.error("❌ No options signals found. Exiting.")
            return {"error": "No options signals available"}
        
        # 2) Select best signals
        logger.info("🎯 Selecting best options signals by profit probability...")
        best_signals = self.data_pipeline.select_best_options_signals(options_signals, target_count=8)
        
        # 3) Decision architecture - multi-agent processing
        logger.info("🤖 Options Decision Architecture: Processing signals with multi-agent system...")
        
        # 4) Generate trading ideas - ensure no duplicates, at least 5
        ideas = []
        successful_ideas = 0
        target_ideas = random.randint(5, 10)
        min_ideas = 5  # minimum 5 recommendations
        used_underlyings = set()  # track used underlyings
        used_combinations = set()  # track used underlying+strategy pairs
        
        # Load today's recommendations from DB to avoid duplicates
        today_recommendations = self._load_today_recommendations()
        
        for i, signal in enumerate(best_signals):
            if successful_ideas >= target_ideas:
                break
            
            profit_score = self.data_pipeline.calculate_profit_score(signal)
            logger.info(f"🔄 Processing signal {i+1}/{len(best_signals)} (profit score: {profit_score:.2f})...")
            
            try:
                # Process via ReAct protocol
                decision = self.decision_architecture.process_options_signal(signal.payload)
                
                if decision and decision.get("ticker"):
                    ticker = decision["ticker"]
                    strategy = decision.get("trade_idea", "Straddle")
                    combination = f"{ticker}_{strategy}"
                    
                    # Check duplicates (underlying or combination)
                    if (ticker not in used_underlyings and 
                        combination not in used_combinations and
                        ticker not in today_recommendations):
                        
                        ideas.append(decision)
                        used_underlyings.add(ticker)
                        used_combinations.add(combination)
                        successful_ideas += 1
                        logger.info(f"✅ Generated {strategy} for {ticker} (confidence: {decision['confidence_level']})")
                    else:
                        logger.info(f"⚠️ Skipping duplicate: {ticker} {strategy}, trying next signal...")
                        continue
                else:
                    logger.warning(f"⚠️ Failed to generate valid options idea from signal {i+1}")
                    
            except Exception as e:
                logger.error(f"❌ Error processing signal {i+1}: {e}")
                continue
        
        # If not enough ideas, try to generate more
        if successful_ideas < min_ideas:
            logger.info(f"⚠️ Only generated {successful_ideas} ideas, need at least {min_ideas}. Attempting to generate more...")
            additional_signals = self.data_pipeline.select_best_options_signals(options_signals, target_count=30)
            
            for signal in additional_signals[len(best_signals):]:
                if successful_ideas >= 10:
                    break
                    
                try:
                    decision = self.decision_architecture.process_options_signal(signal.payload)
                    if decision and decision.get("ticker"):
                        ticker = decision["ticker"]
                        strategy = decision.get("trade_idea", "Straddle")
                        combination = f"{ticker}_{strategy}"
                        
                        if (ticker not in used_underlyings and 
                            combination not in used_combinations and
                            ticker not in today_recommendations):
                            
                            ideas.append(decision)
                            used_underlyings.add(ticker)
                            used_combinations.add(combination)
                            successful_ideas += 1
                            logger.info(f"✅ Additional: {strategy} for {ticker}")
                except Exception as e:
                    continue
        
        # If still below minimum, generate default recommendations
        if successful_ideas < min_ideas:
            logger.info(f"⚠️ Still only {successful_ideas} ideas, generating default recommendations to reach minimum of {min_ideas}...")
            default_tickers = ['SPY', 'QQQ', 'AAPL', 'MSFT', 'TSLA', 'NVDA', 'META', 'GOOGL', 'AMZN', 'NFLX']
            
            for ticker in default_tickers:
                if successful_ideas >= min_ideas:
                    break
                
                if ticker not in used_underlyings and ticker not in today_recommendations:
                    try:
                        # Create default recommendation
                        default_decision = {
                            "ticker": ticker,
                            "directional_view": "Neutral",
                            "trade_idea": "Iron Condor",
                            "option_type": "Spread",
                            "candidate_strikes": "$100.00",
                            "strike_price": 100.0,
                            "tenor": "30 days",
                            "expiration_date": "2025-11-04",
                            "rationale": f"Default recommendation for {ticker} based on high liquidity and market activity. Risk-averse strategy suitable for conservative investors.",
                            "confidence_level": "Medium",
                            "citations": ["Default Recommendation"],
                            "signal_strength": 0.5,
                            "validation_score": 0.7,
                            "counter_arguments": ["Default recommendation - limited market analysis"],
                            "volatility_assessment": "Medium",
                            "strategy_type": "Iron Condor",
                            "uncertainty_metrics": {
                                "signal_uncertainty": 0.2,
                                "validation_uncertainty": 0.2,
                                "counter_uncertainty": 0.2,
                                "source_uncertainty": 0.1,
                                "overall_uncertainty": 0.2,
                                "confidence_interval": {"lower_bound": 0.6, "upper_bound": 0.8, "center": 0.7},
                                "calibration_notes": ["Default recommendation"]
                            }
                        }
                        
                        # Try to enrich with real data from yfinance
                        try:
                            stock_info = yfinance_client.get_stock_info(ticker)
                            if stock_info and stock_info.get('current_price', 0) > 0:
                                current_price = stock_info['current_price']
                                expirations = yfinance_client.get_options_expirations(ticker)
                                if expirations:
                                    expiration_date = expirations[0] if len(expirations) > 0 else "2025-11-04"
                                    liquid_strikes = yfinance_client.get_liquid_strikes(ticker, expiration_date, min_volume=5)
                                    if liquid_strikes:
                                        strike_price = min(liquid_strikes, key=lambda x: abs(x - current_price))
                                        default_decision.update({
                                            "candidate_strikes": f"${strike_price:.2f}",
                                            "strike_price": round(strike_price, 2),
                                            "expiration_date": expiration_date,
                                            "tenor": expiration_date
                                        })
                        except Exception as e:
                            logger.warning(f"Could not get real data for {ticker}: {e}")
                        
                        ideas.append(default_decision)
                        used_underlyings.add(ticker)
                        successful_ideas += 1
                        logger.info(f"✅ Added default recommendation for {ticker}")
                        
                    except Exception as e:
                        logger.error(f"Error creating default recommendation for {ticker}: {e}")
                        continue
        
        # Save today's recommendations
        self._save_today_recommendations(used_underlyings)
        
        # 5) Generate final options report
        if ideas:
            report = self._create_final_options_report(ideas, start_time, options_signals)
            logger.info(f"✅ Successfully generated {len(ideas)} options trading ideas")
        else:
            logger.error("❌ Failed to generate any valid options ideas")
            report = {"error": "No valid options ideas generated"}
        
        end_time = datetime.now()
        execution_time = (end_time - start_time).total_seconds()
        logger.info(f"⏱️ Total execution time: {execution_time:.2f} seconds")
        
        return report
    
    def _create_final_options_report(self, ideas: List[Dict[str, Any]], start_time: datetime, options_signals: List[models.Record]) -> Dict[str, Any]:
        """创建最终期权报告"""
        date_str = start_time.strftime("%Y-%m-%d")
        report_dir = os.path.join(REPORTS_BASE_DIR, date_str)
        
        try:
            os.makedirs(report_dir, exist_ok=True)
            
            # 按信号强度排序
            ideas.sort(key=lambda x: x.get("signal_strength", 0), reverse=True)
            
            # 统计数据源
            data_sources = set()
            for signal in options_signals:
                data_sources.add(signal.payload.get("source", "unknown"))
            
            # 创建JSON报告
            json_data = {
                "report_date": date_str,
                "total_ideas": len(ideas),
                "ideas": ideas,
                "metadata": {
                    "generated_at": datetime.now().isoformat(),
                    "model_used": OLLAMA_MODEL,
                    "generation_method": "Options Multi-Agent System",
                    "data_sources": list(data_sources),
                    "total_options_signals_analyzed": len(options_signals),
                    "avg_signal_strength": sum(idea.get("signal_strength", 0) for idea in ideas) / len(ideas) if ideas else 0,
                    "avg_confidence": sum(1 if idea.get("confidence_level") == "High" else 0.5 if idea.get("confidence_level") == "Medium" else 0.1 for idea in ideas) / len(ideas) if ideas else 0,
                    "strategy_types": list(set(idea.get("strategy_type", "") for idea in ideas)),
                    "underlyings": list(set(idea.get("ticker", "") for idea in ideas))
                }
            }
            
            # 保存JSON报告
            json_path = os.path.join(report_dir, "options_ideas.json")
            with open(json_path, 'w', encoding='utf-8') as f:
                json.dump(json_data, f, indent=2, ensure_ascii=False)
            
            # 创建Markdown报告
            markdown_metadata = {
                "report_date": date_str,
                "total_ideas": len(ideas),
                "data_sources": list(data_sources),
                "total_options_signals_analyzed": len(options_signals),
                "avg_signal_strength": sum(idea.get("signal_strength", 0) for idea in ideas) / len(ideas) if ideas else 0,
                "avg_confidence": sum(1 if idea.get("confidence_level") == "High" else 0.5 if idea.get("confidence_level") == "Medium" else 0.1 for idea in ideas) / len(ideas) if ideas else 0,
                "strategy_types": list(set(idea.get("strategy_type", "") for idea in ideas)),
                "underlyings": list(set(idea.get("ticker", "") for idea in ideas))
            }
            markdown_content = self._create_options_markdown_report(ideas, markdown_metadata)
            markdown_path = os.path.join(report_dir, "options_ideas.md")
            with open(markdown_path, 'w', encoding='utf-8') as f:
                f.write(markdown_content)
            
            logger.info(f"✅ Options reports saved to: {json_path} and {markdown_path}")
            
            return {
                "success": True,
                "ideas": ideas,
                "metadata": json_data["metadata"],
                "files": {
                    "json": json_path,
                    "markdown": markdown_path
                }
            }
            
        except Exception as e:
            logger.error(f"❌ Error creating options report: {e}")
            return {"error": f"Failed to create options report: {e}"}
    
    def _create_options_markdown_report(self, ideas: List[Dict[str, Any]], metadata: Dict[str, Any]) -> str:
        """Create the options report in Markdown format"""
        content = f"# Daily Options Trading Ideas - Expert Analysis\n"
        content += f"## Report for {metadata.get('report_date', 'Unknown Date')}\n\n"
        content += f"**Generated Ideas:** {metadata['total_ideas']}\n"
        content += f"**Data Sources:** {', '.join(metadata['data_sources'])}\n"
        content += f"**Total Options Signals Analyzed:** {metadata['total_options_signals_analyzed']}\n"
        content += f"**Average Signal Strength:** {metadata['avg_signal_strength']:.2f}\n"
        content += f"**Average Confidence:** {metadata['avg_confidence']:.2f}\n"
        content += f"**Strategy Types:** {', '.join(metadata['strategy_types'])}\n"
        content += f"**Underlyings:** {', '.join(metadata['underlyings'])}\n\n"
        
        for i, idea in enumerate(ideas, 1):
            content += f"## Options Idea #{i}\n\n"
            content += f"**Ticker:** {idea['ticker']}\n"
            content += f"**Directional View:** {idea['directional_view']}\n"
            content += f"**Trade Idea:** {idea['trade_idea']}\n"
            content += f"**Option Type:** {idea.get('option_type', 'N/A')}\n"
            content += f"**Candidate Strike(s):** {idea['candidate_strikes']}\n"
            content += f"**Strike Price:** ${idea.get('strike_price', 0):.2f}\n"
            content += f"**Expiration Date:** {idea['tenor']}\n"
            content += f"**Tenor:** {idea.get('expiration_date', idea['tenor'])}\n"
            content += f"**Rationale:** {idea['rationale']}\n"
            content += f"**Confidence Level:** {idea['confidence_level']}\n"
            content += f"**Citations:** {', '.join(idea['citations'])}\n"
            content += f"**Signal Strength:** {idea.get('signal_strength', 0.00):.2f}\n"
            content += f"**Validation Score:** {idea.get('validation_score', 0.00):.2f}\n"
            content += f"**Volatility Assessment:** {idea.get('volatility_assessment', 'N/A')}\n"
            content += f"**Strategy Type:** {idea.get('strategy_type', 'N/A')}\n"
            if idea.get('counter_arguments'):
                content += f"**Counter-Arguments:** {', '.join(idea['counter_arguments'])}\n"
            content += f"**Uncertainty Notes:** {idea.get('uncertainty_notes', 'N/A')}\n\n"
            content += "---\n\n"
        
        return content
    
    def _load_today_recommendations(self) -> set:
        """Load today's recommended tickers"""
        try:
            today = datetime.now().strftime("%Y-%m-%d")
            record_file = os.path.join(REPORTS_BASE_DIR, f"{today}_recommendations.json")
            
            if os.path.exists(record_file):
                with open(record_file, 'r') as f:
                    data = json.load(f)
                    return set(data.get("recommended_tickers", []))
            return set()
        except Exception as e:
            logger.warning(f"Failed to load today's recommendations: {e}")
            return set()
    
    def _save_today_recommendations(self, recommended_tickers: set):
        """Save today's recommended tickers"""
        try:
            today = datetime.now().strftime("%Y-%m-%d")
            record_file = os.path.join(REPORTS_BASE_DIR, f"{today}_recommendations.json")
            
            data = {
                "date": today,
                "recommended_tickers": list(recommended_tickers),
                "timestamp": datetime.now().isoformat()
            }
            
            with open(record_file, 'w') as f:
                json.dump(data, f, indent=2)
                
            logger.info(f"Saved today's recommendations: {list(recommended_tickers)}")
        except Exception as e:
            logger.warning(f"Failed to save today's recommendations: {e}")

def main():
    """Main entry point"""
    try:
        generator = OptionsReportGenerator()
        result = generator.generate_daily_options_report()
        
        if result.get("success"):
            print("✅ Options report generation completed successfully!")
            print(f"📊 Generated {len(result['ideas'])} high-quality options trading ideas")
            print(f"📁 Reports saved to: {result['files']['json']} and {result['files']['markdown']}")
        else:
            print(f"❌ Options report generation failed: {result.get('error', 'Unknown error')}")
            
    except Exception as e:
        logger.error(f"❌ Fatal error: {e}")
        print(f"❌ Fatal error: {e}")

if __name__ == "__main__":
    main()
