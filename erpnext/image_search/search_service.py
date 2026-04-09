"""
Image Search Service
Handles image search across multiple providers (DuckDuckGo, Google, Bing)
"""

import frappe
from datetime import datetime
from typing import List, Dict


class ImageSearchService:
    """Handles image search across multiple providers"""

    def __init__(self):
        self.settings = frappe.get_single("Image Search Settings")

    def search_images(self, query: str, target_count: int = 6) -> List[Dict]:
        """
        Search for images using multiple providers (FREE options prioritized)

        Returns combined and ranked results
        """
        all_images = []

        # FREE OPTION 1: DuckDuckGo (6 images - no API key needed!)
        if self.settings.use_duckduckgo:
            try:
                ddg_images = self.search_duckduckgo(query, num_results=target_count)
                all_images.extend(ddg_images)
                frappe.logger().info(f"DuckDuckGo returned {len(ddg_images)} images for '{query}'")
            except Exception as e:
                frappe.log_error(title="Image Search Error", message=f"DuckDuckGo search failed: {str(e)}")

        # If we have enough images from DuckDuckGo, return them
        if len(all_images) >= target_count:
            unique_images = self._deduplicate_images(all_images)
            ranked_images = self._rank_images(unique_images)
            return ranked_images[:target_count]

        # FALLBACK: Google Custom Search (if configured and DDG didn't return enough)
        if self.settings.google_api_key:
            try:
                needed = target_count - len(all_images)
                google_images = self.search_google(query, num_results=min(needed, 3))
                all_images.extend(google_images)
                frappe.logger().info(f"Google returned {len(google_images)} images for '{query}'")
            except Exception as e:
                frappe.log_error(title="Image Search Error", message=f"Google search failed: {str(e)}")

        # FALLBACK: Bing (if configured and still need more)
        if len(all_images) < target_count and self.settings.bing_api_key:
            try:
                needed = target_count - len(all_images)
                bing_images = self.search_bing(query, num_results=min(needed, 3))
                all_images.extend(bing_images)
                frappe.logger().info(f"Bing returned {len(bing_images)} images for '{query}'")
            except Exception as e:
                frappe.log_error(title="Image Search Error", message=f"Bing search failed: {str(e)}")

        # Deduplicate and rank
        unique_images = self._deduplicate_images(all_images)
        ranked_images = self._rank_images(unique_images)

        return ranked_images[:target_count]

    def search_duckduckgo(self, query: str, num_results: int = 6) -> List[Dict]:
        """
        Search DuckDuckGo Images (100% FREE - No API key needed!)

        Uses the DuckDuckGo interface which doesn't require authentication.
        This is the primary method since it's completely free.
        """
        start_time = datetime.now()

        try:
            from ddgs import DDGS

            with DDGS() as ddgs:
                results = list(ddgs.images(
                    query=query,
                    max_results=num_results,
                    size="Medium",  # Medium = 200-500px
                    type_image=None,
                    layout=None,
                    license_image=None
                ))

            response_time = (datetime.now() - start_time).total_seconds() * 1000

            # Log API call (even though it's free, track usage)
            self._log_api_call(
                api_provider="DuckDuckGo Images",
                search_query=query,
                response_code=200,
                response_time_ms=int(response_time),
                results_count=len(results),
                error_message=None
            )

            images = []
            for item in results:
                # DuckDuckGo returns good quality thumbnails and full images
                # Convert width/height to int (sometimes returned as strings)
                width = int(item.get('width', 0)) if item.get('width') else 0
                height = int(item.get('height', 0)) if item.get('height') else 0

                images.append({
                    "url": item.get('image'),
                    "thumbnail_url": item.get('thumbnail'),
                    "source": "DuckDuckGo Images",
                    "width": width,
                    "height": height,
                    "quality_score": self._calculate_quality_score(width, height),
                    "metadata": {
                        "title": item.get('title'),
                        "source_url": item.get('url')
                    }
                })

            return images

        except ImportError:
            error_msg = "ddgs package not installed. Install with: pip install ddgs"
            frappe.log_error(error_msg, "Image Search - Missing Dependency")
            self._log_api_call(
                api_provider="DuckDuckGo Images",
                search_query=query,
                response_code=0,
                response_time_ms=0,
                results_count=0,
                error_message=error_msg
            )
            raise RuntimeError(error_msg)
        except Exception as e:
            error_msg = str(e)
            self._log_api_call(
                api_provider="DuckDuckGo Images",
                search_query=query,
                response_code=0,
                response_time_ms=0,
                results_count=0,
                error_message=error_msg
            )
            raise e

    def search_google(self, query: str, num_results: int = 3) -> List[Dict]:
        """Search Google Custom Search API (Optional - Paid)"""
        import requests
        start_time = datetime.now()

        url = "https://www.googleapis.com/customsearch/v1"
        params = {
            "key": self.settings.get_password("google_api_key"),
            "cx": self.settings.google_cx,
            "q": query,
            "searchType": "image",
            "num": num_results,
            "imgSize": "medium",
            "safe": "active"
        }

        try:
            response = requests.get(url, params=params, timeout=10)
            response_time = (datetime.now() - start_time).total_seconds() * 1000

            # Log API call
            self._log_api_call(
                api_provider="Google Custom Search",
                search_query=query,
                response_code=response.status_code,
                response_time_ms=int(response_time),
                results_count=num_results if response.status_code == 200 else 0,
                error_message=None if response.status_code == 200 else response.text
            )

            response.raise_for_status()
            data = response.json()

            images = []
            for item in data.get('items', [])[:num_results]:
                width = item.get('image', {}).get('width', 0)
                height = item.get('image', {}).get('height', 0)

                images.append({
                    "url": item['link'],
                    "thumbnail_url": item.get('image', {}).get('thumbnailLink'),
                    "source": "Google Images",
                    "width": width,
                    "height": height,
                    "quality_score": self._calculate_quality_score(width, height),
                    "metadata": {
                        "title": item.get('title'),
                        "context_link": item.get('image', {}).get('contextLink')
                    }
                })

            return images

        except Exception as e:
            self._log_api_call(
                api_provider="Google Custom Search",
                search_query=query,
                response_code=0,
                response_time_ms=0,
                results_count=0,
                error_message=str(e)
            )
            raise e

    def search_bing(self, query: str, num_results: int = 3) -> List[Dict]:
        """Search Bing Image Search API (Optional - Paid)"""
        import requests
        start_time = datetime.now()

        url = "https://api.bing.microsoft.com/v7.0/images/search"
        headers = {"Ocp-Apim-Subscription-Key": self.settings.get_password("bing_api_key")}
        params = {
            "q": query,
            "count": num_results,
            "imageType": "Photo",
            "size": "Medium",
            "safeSearch": "Strict"
        }

        try:
            response = requests.get(url, headers=headers, params=params, timeout=10)
            response_time = (datetime.now() - start_time).total_seconds() * 1000

            # Log API call
            self._log_api_call(
                api_provider="Bing Image Search",
                search_query=query,
                response_code=response.status_code,
                response_time_ms=int(response_time),
                results_count=num_results if response.status_code == 200 else 0,
                error_message=None if response.status_code == 200 else response.text
            )

            response.raise_for_status()
            data = response.json()

            images = []
            for item in data.get('value', [])[:num_results]:
                images.append({
                    "url": item['contentUrl'],
                    "thumbnail_url": item.get('thumbnailUrl'),
                    "source": "Bing Images",
                    "width": item.get('width', 0),
                    "height": item.get('height', 0),
                    "quality_score": self._calculate_quality_score(
                        item.get('width', 0),
                        item.get('height', 0)
                    ),
                    "metadata": {
                        "name": item.get('name'),
                        "host_page_url": item.get('hostPageUrl'),
                        "encoding_format": item.get('encodingFormat')
                    }
                })

            return images

        except Exception as e:
            self._log_api_call(
                api_provider="Bing Image Search",
                search_query=query,
                response_code=0,
                response_time_ms=0,
                results_count=0,
                error_message=str(e)
            )
            raise e

    def _calculate_quality_score(self, width: int, height: int) -> float:
        """
        Calculate quality score based on image dimensions

        Scoring (optimized for 200x200 target):
        - 500x500+: 1.0 (excellent)
        - 300x300-499x499: 0.9 (very good)
        - 200x200-299x299: 0.8 (good - target size)
        - 150x150-199x199: 0.6 (acceptable)
        - Below 150x150: 0.3 (poor)
        """
        if not width or not height:
            return 0.5

        min_dimension = min(width, height)

        if min_dimension >= 500:
            return 1.0
        elif min_dimension >= 300:
            return 0.9
        elif min_dimension >= 200:
            return 0.8
        elif min_dimension >= 150:
            return 0.6
        else:
            return 0.3

    def _deduplicate_images(self, images: List[Dict]) -> List[Dict]:
        """Remove duplicate images based on URL"""
        seen_urls = set()
        unique_images = []

        for image in images:
            if image['url'] not in seen_urls:
                seen_urls.add(image['url'])
                unique_images.append(image)

        return unique_images

    def _rank_images(self, images: List[Dict]) -> List[Dict]:
        """Rank images by quality score"""
        return sorted(images, key=lambda x: x['quality_score'], reverse=True)

    def _log_api_call(self,
                     api_provider: str,
                     search_query: str,
                     response_code: int,
                     response_time_ms: int,
                     results_count: int,
                     error_message: str = None):
        """Log API call for monitoring and rate limiting"""
        try:
            log = frappe.get_doc({
                "doctype": "Image Search API Log",
                "api_provider": api_provider,
                "request_timestamp": frappe.utils.now(),
                "search_query": search_query,
                "response_code": response_code,
                "results_count": results_count,
                "response_time_ms": response_time_ms,
                "error_message": error_message,
                "quota_used": 1
            })
            log.insert(ignore_permissions=True)
            frappe.db.commit()
        except Exception as e:
            frappe.log_error(f"Error logging API call: {str(e)}", "Image Search - Log Error")
