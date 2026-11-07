import csv
import tempfile
import unittest
from pathlib import Path

from recipe01_stage import build_review_queue, summary


class StageReviewTests(unittest.TestCase):
    def test_exact_name_is_still_unverified(self):
        with tempfile.TemporaryDirectory() as tmp:
            source=Path(tmp)/'candidates.csv'
            with source.open('w',encoding='utf-8-sig',newline='') as fp:
                writer=csv.DictWriter(fp,fieldnames=['source_file','source_page','printed_name',
                    'protein_g_100g','fat_g_100g','carbohydrate_g_100g','kcal_100g','review_status'])
                writer.writeheader()
                writer.writerow(dict(source_file='book.pdf',source_page='2',printed_name='Молоко 2,5%',
                      protein_g_100g='3',fat_g_100g='2.5',carbohydrate_g_100g='4.8',
                      kcal_100g='52',review_status='candidate_unverified'))
            rows=build_review_queue(source,[('P1','Молоко 2,5%')])
            self.assertEqual(rows[0]['matched_product_id'],'P1')
            self.assertEqual(rows[0]['match_status'],'exact_name_candidate')
            self.assertEqual(rows[0]['promotion_status'],'needs_manual_review')
            self.assertEqual(summary(rows)['exact_name_candidate'],1)

    def test_ambiguous_identity_never_picks_arbitrary_id(self):
        with tempfile.TemporaryDirectory() as tmp:
            source=Path(tmp)/'candidates.csv'
            source.write_text('source_file,source_page,printed_name,protein_g_100g,fat_g_100g,carbohydrate_g_100g,kcal_100g,review_status\nbook,1,Рис,7,1,73,330,candidate_unverified\n',encoding='utf-8')
            row=build_review_queue(source,[('a','Рис'),('b','рис')])[0]
            self.assertEqual(row['match_status'],'ambiguous_name')
            self.assertEqual(row['matched_product_id'],'')

    def test_rejects_unexpected_promotion(self):
        with tempfile.TemporaryDirectory() as tmp:
            source=Path(tmp)/'candidates.csv'
            source.write_text('source_file,source_page,printed_name,protein_g_100g,fat_g_100g,carbohydrate_g_100g,kcal_100g,review_status\nbook,1,Рис,7,1,73,330,verified\n',encoding='utf-8')
            with self.assertRaises(ValueError):
                build_review_queue(source,[])
