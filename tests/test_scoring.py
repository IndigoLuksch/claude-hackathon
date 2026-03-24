import asyncio
import unittest
from unittest.mock import AsyncMock, MagicMock, patch
from app.scoring import score_and_persist, _signal_ownership_opacity
from app.models import Vessel

class TestScoring(unittest.TestCase):
    def test_ownership_opacity_signal(self):
        # Unverified -> 5 pts
        pts, status = _signal_ownership_opacity("US", False)
        self.assertEqual(pts, 5.0)
        self.assertEqual(status, "unverified")
        
        # Verified + FOC -> 5 pts
        pts, status = _signal_ownership_opacity("PA", True)
        self.assertEqual(pts, 5.0)
        self.assertEqual(status, "foc")
        
        # Verified + Non-FOC -> 0 pts
        pts, status = _signal_ownership_opacity("US", True)
        self.assertEqual(pts, 0.0)
        self.assertEqual(status, "verified")

    def test_score_and_persist_includes_ownership(self):
        async def run_test():
            # Mock database session
            mock_db = AsyncMock()
            
            # Patch all signal functions in app.scoring
            with patch("app.scoring._signal_gaps", new_callable=AsyncMock) as m_gaps, \
                 patch("app.scoring._signal_loitering", new_callable=AsyncMock) as m_loiter, \
                 patch("app.scoring._signal_encounters", new_callable=AsyncMock) as m_enc, \
                 patch("app.scoring._signal_rfmo_absent", new_callable=AsyncMock) as m_rfmo, \
                 patch("app.scoring._signal_flag_changes", return_value=(0.0, 0)) as m_flags, \
                 patch("app.scoring._signal_sanctions", return_value=0.0) as m_sanctions:
                
                m_gaps.return_value = (0.0, 0)
                m_loiter.return_value = (0.0, 0)
                m_enc.return_value = (0.0, 0)
                m_rfmo.return_value = (0.0, "authorised")

                # Test case 1: Unverified US flag
                vessel = Vessel(mmsi="123", flag_state="US", ownership_verified=False)
                mock_db.get.return_value = vessel
                
                score, tier = await score_and_persist("123", mock_db)
                self.assertEqual(score, 5.0)
                self.assertEqual(vessel.risk_score, 5.0)

                # Test case 2: Verified PA flag (FOC)
                vessel_foc = Vessel(mmsi="456", flag_state="PA", ownership_verified=True)
                mock_db.get.return_value = vessel_foc
                
                score, tier = await score_and_persist("456", mock_db)
                self.assertEqual(score, 5.0)
                self.assertEqual(vessel_foc.risk_score, 5.0)

                # Test case 3: Verified US flag
                vessel_ok = Vessel(mmsi="789", flag_state="US", ownership_verified=True)
                mock_db.get.return_value = vessel_ok
                
                score, tier = await score_and_persist("789", mock_db)
                self.assertEqual(score, 0.0)
                self.assertEqual(vessel_ok.risk_score, 0.0)

        asyncio.run(run_test())

if __name__ == "__main__":
    unittest.main()
