"""
Usage Tracker Module for LinkedIn Enricher
Tracks API consumption across all providers: Crustdata, PhantomBuster, SalesQL, OpenAI
"""

import time
from datetime import datetime
from typing import Optional
from functools import wraps


# OpenAI pricing (per 1M tokens) - gpt-4o-mini
OPENAI_PRICING = {
    'gpt-4o-mini': {
        'input': 0.15,   # $0.15 per 1M input tokens
        'output': 0.60,  # $0.60 per 1M output tokens
    },
    'gpt-4o': {
        'input': 2.50,   # $2.50 per 1M input tokens
        'output': 10.00, # $10.00 per 1M output tokens
    }
}


class UsageTracker:
    """Tracks and logs API usage to Supabase."""

    def __init__(self, db_client=None):
        """Initialize tracker with optional database client.

        Args:
            db_client: SupabaseClient instance for logging to database
        """
        self.db_client = db_client

    def log_usage(
        self,
        provider: str,
        operation: str,
        request_count: int = 1,
        credits_used: float = None,
        tokens_input: int = None,
        tokens_output: int = None,
        cost_usd: float = None,
        status: str = 'success',
        error_message: str = None,
        response_time_ms: int = None,
        metadata: dict = None
    ) -> Optional[dict]:
        """Log an API usage event.

        Args:
            provider: API provider name (crustdata, phantombuster, salesql, openai)
            operation: Operation type (enrich, scrape, email_lookup, screen, etc.)
            request_count: Number of API requests made
            credits_used: Credits/lookups consumed (for credit-based APIs)
            tokens_input: Input tokens (for OpenAI)
            tokens_output: Output tokens (for OpenAI)
            cost_usd: Calculated cost in USD
            status: 'success' or 'error'
            error_message: Error details if status is 'error'
            response_time_ms: API response time in milliseconds
            metadata: Additional JSON metadata

        Returns:
            The inserted record or None if no db_client
        """
        if not self.db_client:
            return None

        data = {
            'provider': provider.lower(),
            'operation': operation,
            'request_count': request_count,
            'status': status,
            'created_at': datetime.utcnow().isoformat(),
        }

        if credits_used is not None:
            data['credits_used'] = credits_used
        if tokens_input is not None:
            data['tokens_input'] = tokens_input
        if tokens_output is not None:
            data['tokens_output'] = tokens_output
        if cost_usd is not None:
            data['cost_usd'] = cost_usd
        if error_message:
            data['error_message'] = error_message
        if response_time_ms is not None:
            data['response_time_ms'] = response_time_ms
        if metadata:
            data['metadata'] = metadata

        try:
            result = self.db_client.insert('api_usage_logs', data)
            return result[0] if result else None
        except Exception as e:
            # Don't let logging failures break the app
            print(f"[UsageTracker] Failed to log usage: {e}")
            return None

    def log_crustdata(
        self,
        profiles_enriched: int,
        status: str = 'success',
        error_message: str = None,
        response_time_ms: int = None
    ) -> Optional[dict]:
        """Log Crustdata enrichment usage.

        Crustdata charges 3 credits per profile enriched.
        """
        return self.log_usage(
            provider='crustdata',
            operation='enrich',
            request_count=1,
            credits_used=profiles_enriched * 3,  # 3 credits per profile
            status=status,
            error_message=error_message,
            response_time_ms=response_time_ms,
            metadata={'profiles_enriched': profiles_enriched}
        )

    def log_salesql(
        self,
        lookups: int = 1,
        emails_found: int = 0,
        status: str = 'success',
        error_message: str = None,
        response_time_ms: int = None
    ) -> Optional[dict]:
        """Log SalesQL email lookup usage.

        SalesQL has 5000 lookups/day limit.
        """
        return self.log_usage(
            provider='salesql',
            operation='email_lookup',
            request_count=lookups,
            credits_used=lookups,  # Each lookup counts against daily limit
            status=status,
            error_message=error_message,
            response_time_ms=response_time_ms,
            metadata={'emails_found': emails_found}
        )

    def log_openai(
        self,
        tokens_input: int,
        tokens_output: int,
        model: str = 'gpt-4o-mini',
        profiles_screened: int = 1,
        status: str = 'success',
        error_message: str = None,
        response_time_ms: int = None
    ) -> Optional[dict]:
        """Log OpenAI API usage with cost calculation.

        Args:
            tokens_input: Number of input tokens
            tokens_output: Number of output tokens
            model: Model name for pricing lookup
            profiles_screened: Number of profiles screened in this call
        """
        # Calculate cost
        pricing = OPENAI_PRICING.get(model, OPENAI_PRICING['gpt-4o-mini'])
        cost_usd = (
            (tokens_input / 1_000_000) * pricing['input'] +
            (tokens_output / 1_000_000) * pricing['output']
        )

        return self.log_usage(
            provider='openai',
            operation='screen',
            request_count=1,
            tokens_input=tokens_input,
            tokens_output=tokens_output,
            cost_usd=cost_usd,
            status=status,
            error_message=error_message,
            response_time_ms=response_time_ms,
            metadata={
                'model': model,
                'profiles_screened': profiles_screened
            }
        )

    def log_phantombuster(
        self,
        operation: str = 'scrape',
        profiles_scraped: int = 0,
        status: str = 'success',
        error_message: str = None,
        agent_id: str = None,
        container_id: str = None
    ) -> Optional[dict]:
        """Log PhantomBuster usage.

        PhantomBuster uses credits based on execution time and phantom type.
        """
        return self.log_usage(
            provider='phantombuster',
            operation=operation,
            request_count=1,
            credits_used=1,  # 1 run = 1 credit (simplified)
            status=status,
            error_message=error_message,
            metadata={
                'profiles_scraped': profiles_scraped,
                'agent_id': agent_id,
                'container_id': container_id
            }
        )


def calculate_openai_cost(tokens_input: int, tokens_output: int, model: str = 'gpt-4o-mini') -> float:
    """Calculate OpenAI API cost in USD.

    Args:
        tokens_input: Number of input tokens
        tokens_output: Number of output tokens
        model: Model name

    Returns:
        Cost in USD
    """
    pricing = OPENAI_PRICING.get(model, OPENAI_PRICING['gpt-4o-mini'])
    return (
        (tokens_input / 1_000_000) * pricing['input'] +
        (tokens_output / 1_000_000) * pricing['output']
    )


def track_api_call(tracker: UsageTracker, provider: str, operation: str):
    """Decorator to track API calls with timing.

    Usage:
        @track_api_call(tracker, 'crustdata', 'enrich')
        def my_api_function():
            ...
    """
    def decorator(func):
        @wraps(func)
        def wrapper(*args, **kwargs):
            start_time = time.time()
            try:
                result = func(*args, **kwargs)
                elapsed_ms = int((time.time() - start_time) * 1000)
                tracker.log_usage(
                    provider=provider,
                    operation=operation,
                    response_time_ms=elapsed_ms,
                    status='success'
                )
                return result
            except Exception as e:
                elapsed_ms = int((time.time() - start_time) * 1000)
                tracker.log_usage(
                    provider=provider,
                    operation=operation,
                    response_time_ms=elapsed_ms,
                    status='error',
                    error_message=str(e)[:500]
                )
                raise
        return wrapper
    return decorator
