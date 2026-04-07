"""
Image Search Worker
Background worker that processes image search jobs
"""

import json
import frappe
from frappe.utils.background_jobs import enqueue
from typing import List, Dict
import time


class ImageSearchWorker:
    """Background worker that processes image search jobs"""

    def __init__(self):
        from erpnext.image_search.queue_manager import ImageSearchQueueManager
        from erpnext.image_search.search_service import ImageSearchService

        self.queue_manager = ImageSearchQueueManager()
        self.search_service = ImageSearchService()

    def start_worker(self):
        """Start processing jobs from the queue"""
        enqueue(
            'erpnext.image_search.worker.process_job_batch',
            queue='default',
            timeout=600,
            is_async=True
        )

    def process_job_batch(self, batch_size: int = 10):
        """Process a batch of image search jobs"""
        jobs = self.queue_manager.get_next_batch(batch_size)

        if not jobs:
            frappe.logger().info("No image search jobs in queue")
            return

        frappe.logger().info(f"Processing {len(jobs)} image search jobs")

        for job in jobs:
            try:
                self.process_single_job(job)
                # Rate limiting: 2 seconds between requests (DuckDuckGo)
                time.sleep(2)
            except Exception as e:
                frappe.log_error(
                    f"Error processing job {job.name}: {str(e)}",
                    "Image Search Worker Error"
                )
                self.queue_manager.mark_job_failed(job.name, str(e))

        # Schedule next batch if there are more jobs
        pending_count = frappe.db.count(
            "Product Image Search Job",
            {"status": ["in", ["Pending", "Retrying"]]}
        )

        if pending_count > 0:
            frappe.logger().info(
                f"{pending_count} jobs remaining, scheduling next batch"
            )
            enqueue(
                'erpnext.image_search.worker.process_job_batch',
                queue='default',
                timeout=600,
                is_async=True,
                batch_size=batch_size
            )

    def process_single_job(self, job: Dict):
        """Process a single image search job"""
        job_name = job['name']

        self.queue_manager.mark_job_started(job_name)

        try:
            # Search for images
            images = self.search_service.search_images(
                query=job['search_query'],
                target_count=6
            )

            # Save image candidates
            saved_count = 0
            for rank, image_data in enumerate(images[:6], start=1):
                try:
                    self._save_image_candidate(
                        product_type=job['product_type'],
                        product_id=job['product_id'],
                        image_data=image_data,
                        rank=rank
                    )
                    saved_count += 1
                except Exception as e:
                    frappe.log_error(
                        f"Error saving image candidate: {str(e)}",
                        "Image Search Worker - Save Error"
                    )

            # Mark job as completed
            self.queue_manager.mark_job_completed(job_name, saved_count)

            frappe.logger().info(
                f"Completed job {job_name} for {job['product_name']}: {saved_count} images found"
            )

        except Exception as e:
            raise e

    def _save_image_candidate(self,
                             product_type: str,
                             product_id: str,
                             image_data: Dict,
                             rank: int):
        """Save an image candidate to the database"""
        candidate = frappe.get_doc({
            "doctype": "Product Image Candidate",
            "product_type": product_type,
            "product_id": product_id,
            "image_url": image_data['url'],
            "thumbnail_url": image_data.get('thumbnail_url'),
            "source": image_data['source'],
            "width": image_data.get('width'),
            "height": image_data.get('height'),
            "quality_score": image_data.get('quality_score', 0.5),
            "rank": rank,
            "metadata": json.dumps(image_data.get('metadata', {}))
        })
        candidate.insert(ignore_permissions=True)
        frappe.db.commit()


# Module-level functions for enqueue

def process_job_batch(batch_size=10):
    """Process a batch of image search jobs - callable by enqueue"""
    worker = ImageSearchWorker()
    worker.process_job_batch(batch_size)


def start_worker():
    """Start the image search worker - callable by enqueue"""
    worker = ImageSearchWorker()
    worker.start_worker()
