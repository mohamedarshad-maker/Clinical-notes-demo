"""WHO ICD-11 MMS service with OAuth authentication."""
import asyncio
import html
import logging
import re
from datetime import datetime, timedelta
from typing import List, Dict, Optional
import httpx
from app.config import settings

logger = logging.getLogger(__name__)

# WHO ICD-11 API endpoints
TOKEN_URL = "https://icdaccessmanagement.who.int/connect/token"
API_BASE_URL = "https://id.who.int/icd"

# Token cache
_token_cache: Optional[Dict] = None
_token_lock = asyncio.Lock()

# Lookup cache (in-memory cache keyed by release, language, and normalized query)
_lookup_cache: Dict[str, List[Dict]] = {}


class WHOICDError(Exception):
    """WHO ICD service error."""
    pass


class WHOICDService:
    """Manages WHO ICD-11 MMS lookups with OAuth."""

    @property
    def client_id(self) -> Optional[str]:
        return settings.who_icd_client_id

    @property
    def client_secret(self) -> Optional[str]:
        return settings.who_icd_client_secret

    @property
    def release(self) -> str:
        return settings.who_icd_release

    @property
    def language(self) -> str:
        return settings.who_icd_language

    def is_configured(self) -> bool:
        """Check if WHO ICD is configured with real credentials."""
        return settings.is_who_icd_configured

    def sanitize_diagnosis_query(self, raw_query: str) -> str:
        """
        Sanitize diagnosis phrase before calling WHO API:
        strip HTML, emails, phone numbers, MRN-like strings, and excess whitespace.
        """
        text = re.sub(r"<[^>]+>", " ", raw_query or "")
        # Remove email addresses
        text = re.sub(r"\b[\w.+-]+@[\w-]+\.[\w.-]+\b", "", text)
        # Remove phone/MRN-like long digit sequences (5+ digits)
        text = re.sub(r"\b\d{5,}\b", "", text)
        # Collapse whitespace
        text = re.sub(r"\s+", " ", text).strip(" ,;:-")
        return text[:200]

    def _strip_html(self, text: Optional[str]) -> str:
        """Remove HTML highlight tags (like <em class='found'>) from WHO API titles."""
        if not text:
            return ""
        cleaned = re.sub(r"<[^>]+>", "", text)
        return html.unescape(cleaned).strip()

    async def get_access_token(self) -> str:
        """
        Get OAuth2 access token with caching.
        """
        if not self.is_configured():
            raise WHOICDError("WHO ICD credentials not configured")

        async with _token_lock:
            global _token_cache

            if _token_cache is not None:
                expires_at = _token_cache.get("expires_at")
                if expires_at and datetime.utcnow() < expires_at:
                    return _token_cache["access_token"]

            try:
                async with httpx.AsyncClient(timeout=20.0) as client:
                    response = await client.post(
                        TOKEN_URL,
                        data={
                            "client_id": self.client_id,
                            "client_secret": self.client_secret,
                            "scope": "icdapi_access",
                            "grant_type": "client_credentials"
                        },
                        headers={
                            "Content-Type": "application/x-www-form-urlencoded"
                        }
                    )

                    response.raise_for_status()
                    data = response.json()

                    access_token = data["access_token"]
                    expires_in = int(data.get("expires_in", 3600))

                    # Refresh 5 minutes early (or half lifetime if short)
                    margin = min(300, max(30, expires_in // 2))
                    expires_at = datetime.utcnow() + timedelta(seconds=expires_in - margin)

                    _token_cache = {
                        "access_token": access_token,
                        "expires_at": expires_at
                    }

                    logger.info("Obtained new WHO ICD access token")
                    return access_token

            except httpx.HTTPStatusError as e:
                logger.error(f"WHO ICD authentication failed: {e}")
                raise WHOICDError(
                    f"Authentication failed: {e.response.status_code}")
            except Exception as e:
                logger.error(f"WHO ICD token request failed: {e}")
                raise WHOICDError(f"Token request failed: {e}")

    async def search_diagnosis(
        self,
        diagnosis_text: str,
        max_results: int = 5
    ) -> List[Dict]:
        """
        Search ICD-11 MMS for diagnosis codes.
        Validates that returned candidates are valid MMS stem codes (not uncoded or Chapter X extension codes).
        """
        if not self.is_configured():
            raise WHOICDError("WHO ICD credentials not configured")

        sanitized_query = self.sanitize_diagnosis_query(diagnosis_text)
        if not sanitized_query:
            return []

        cache_key = f"{self.release}:{self.language}:{sanitized_query.lower()}"
        if cache_key in _lookup_cache:
            logger.info(f"WHO ICD cache hit for: {sanitized_query}")
            return _lookup_cache[cache_key][:max_results]

        # Official WHO ICD-11 v2 MMS search endpoint includes /11/
        search_url = f"{API_BASE_URL}/release/11/{self.release}/mms/search"

        try:
            token = await self.get_access_token()

            async with httpx.AsyncClient(timeout=20.0) as client:
                params = {
                    "q": sanitized_query,
                    "flatResults": "true",
                    "useFlexisearch": "true"
                }
                headers = {
                    "Authorization": f"Bearer {token}",
                    "Accept": "application/json",
                    "Accept-Language": self.language,
                    "API-Version": "v2"
                }

                response = await client.get(search_url, params=params, headers=headers)

                if response.status_code == 401:
                    logger.info("WHO ICD token expired, refreshing...")
                    async with _token_lock:
                        global _token_cache
                        _token_cache = None
                    token = await self.get_access_token()
                    headers["Authorization"] = f"Bearer {token}"
                    response = await client.get(search_url, params=params, headers=headers)

                response.raise_for_status()
                data = response.json()

                candidates = []
                destinations = data.get("destinationEntities", []) or []

                for entity in destinations:
                    mms_code = (entity.get("theCode")
                                or entity.get("code") or "").strip()
                    # Reject uncoded / Foundation-only grouping entities
                    if not mms_code:
                        continue

                    # Reject Chapter X extension codes as standalone primary diagnosis codes
                    chapter = str(entity.get("chapter") or "").strip().upper()
                    if mms_code.upper().startswith("X") or chapter == "X":
                        continue

                    title = self._strip_html(entity.get("title") or "Unknown")
                    entity_uri = entity.get("id") or entity.get("stemId") or ""

                    if entity_uri.startswith("http://"):
                        entity_uri_https = "https://" + \
                            entity_uri[len("http://"):]
                    else:
                        entity_uri_https = entity_uri

                    who_source_link = (
                        entity_uri_https
                        if entity_uri_https.startswith("http")
                        else f"{API_BASE_URL}/release/11/{self.release}/mms/{entity_uri_https}"
                    )

                    candidates.append({
                        "mms_code": mms_code,
                        "mms_title": title,
                        "mms_description": self._strip_html(entity.get("definition")) or None,
                        "foundation_uri": entity_uri_https or None,
                        "who_source_link": who_source_link,
                        "search_rank": len(candidates) + 1
                    })

                    if len(candidates) >= max_results:
                        break

                _lookup_cache[cache_key] = candidates
                logger.info(
                    f"WHO ICD search found {len(candidates)} valid MMS candidates for: {sanitized_query}")
                return candidates

        except httpx.HTTPStatusError as e:
            logger.error(f"WHO ICD search failed: {e}")
            if e.response.status_code == 404:
                return []
            raise WHOICDError(f"Search failed: {e.response.status_code}")
        except WHOICDError:
            raise
        except Exception as e:
            logger.error(f"WHO ICD search error: {e}")
            raise WHOICDError(f"Search error: {e}")

    async def get_entity_details(self, entity_id: str) -> Optional[Dict]:
        """Get detailed information about an ICD entity."""
        if not self.is_configured():
            raise WHOICDError("WHO ICD credentials not configured")

        try:
            token = await self.get_access_token()

            if entity_id.startswith("http://"):
                url = "https://" + entity_id[len("http://"):]
            elif entity_id.startswith("https://"):
                url = entity_id
            else:
                url = f"{API_BASE_URL}/release/11/{self.release}/mms/{entity_id}"

            async with httpx.AsyncClient(timeout=20.0) as client:
                response = await client.get(
                    url,
                    headers={
                        "Authorization": f"Bearer {token}",
                        "Accept": "application/json",
                        "Accept-Language": self.language,
                        "API-Version": "v2"
                    }
                )

                if response.status_code == 404:
                    return None

                response.raise_for_status()
                return response.json()

        except Exception as e:
            logger.error(f"WHO ICD entity fetch error: {e}")
            return None

    async def lookup_multiple(
        self,
        diagnoses: List[str],
        max_results_per_diagnosis: int = 5
    ) -> Dict[str, List[Dict]]:
        """Lookup multiple diagnoses with bounded concurrency."""
        if not diagnoses:
            return {}

        semaphore = asyncio.Semaphore(3)

        async def bounded_search(diagnosis: str):
            async with semaphore:
                try:
                    results = await self.search_diagnosis(diagnosis, max_results_per_diagnosis)
                    return diagnosis, results
                except Exception as e:
                    logger.error(f"Failed to lookup '{diagnosis}': {e}")
                    return diagnosis, []

        tasks = [bounded_search(d) for d in diagnoses]
        results = await asyncio.gather(*tasks)
        return dict(results)


# Singleton instance
who_icd_service = WHOICDService()
