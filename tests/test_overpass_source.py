import json, unittest
from unittest.mock import patch
from scout.scout_agent import OverpassRestaurantSource, Target

PAYLOAD={"elements":[{"type":"node","id":123,"tags":{"name":"Real Data Test Restaurant","amenity":"restaurant","phone":"+234 800 000 0099","website":"https://real-data-test.example.invalid","addr:street":"Fixture Road"}},{"type":"way","id":456,"tags":{"amenity":"restaurant"}}]}

class Response:
 status=200
 def read(self): return json.dumps(PAYLOAD).encode()
 def __enter__(self): return self
 def __exit__(self,*args): return False

class OverpassSourceTests(unittest.TestCase):
 def test_bounded_public_candidate_preserves_evidence_without_network(self):
  source=OverpassRestaurantSource(min_delay_seconds=0)
  with patch("scout.scout_agent.urlopen",return_value=Response()) as request:
   rows=source.discover(Target("Nigeria","Lagos","Restaurants",lead_limit=5,dry_run=True))
  self.assertEqual(len(rows),1); self.assertEqual(rows[0]["data_classification"],"REAL_PUBLIC_DATA"); self.assertEqual(rows[0]["source_type"],"OPENSTREETMAP_ELEMENT"); self.assertIn("openstreetmap.org/node/123",rows[0]["source_url"]); self.assertEqual(request.call_count,1)
 def test_scope_and_request_limit_fail_closed(self):
  source=OverpassRestaurantSource(max_requests=0,min_delay_seconds=0)
  with self.assertRaisesRegex(RuntimeError,"limit"): source.discover(Target("Nigeria","Lagos","Restaurants",lead_limit=5))
  with self.assertRaisesRegex(ValueError,"Lagos"): OverpassRestaurantSource().discover(Target("Nigeria","Abuja","Restaurants",lead_limit=5))
