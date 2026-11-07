import unittest
from recipe01_reference_match import find_reference_rows


class ReferenceMatchTests(unittest.TestCase):
    def test_exact_label_does_not_infer_raw_or_ready(self):
        pdf_text='''Рис белый шлифованный   7   1   74   333
Рис белый шлифованный вареный   2,3   0,2   28,7   130
Яблоки   0,4   0,4   9,8   47\f
Хек   16,6   2,2   -   86'''
        results=find_reference_rows(pdf_text,['рис','яблоки','хек','бананы'])
        self.assertEqual(1,len(results['рис']))
        self.assertEqual('Рис белый шлифованный',results['рис'][0]['source_label'])
        self.assertEqual('unknown',results['рис'][0]['food_state'])
        self.assertEqual('needs_editorial_review',results['рис'][0]['measurement_basis'])
        self.assertEqual(2,results['хек'][0]['source_page'])
        self.assertEqual((),results['бананы'])

    def test_raw_and_cooked_variants_stay_separate_unverified_rows(self):
        sample=('Макаронные изделия в/с   10,4   1,1   69,7   337\n'
                'Макаронные изделия в/с,вареные   3,5   0,4   23,2   112')
        items=find_reference_rows(sample,['макароны'])['макароны']
        self.assertEqual(2,len(items))
        self.assertEqual({'unverified'},{item['review_status'] for item in items})
        self.assertTrue(all(item['food_state']=='unknown' for item in items))

    def test_dashes_and_ranges_are_not_replaced_with_zero_or_point_values(self):
        text=('Рис белый шлифованный   7   1   74   333\n'
              'Рис белый шлифованныйвареный   2,3   0,2   28,7   130\n'
              'Минтай   15,9   0,9   -   72\n'
              'Масло сливочное   0,5-1,0   72,5-82,5   0,8-1,4   661-748')
        result=find_reference_rows(text,['рис','минтай','масло сливочное'])
        self.assertEqual(1,len(result['рис']))
        self.assertEqual('-',result['минтай'][0]['raw_values'][2])
        self.assertEqual('0,5-1,0',result['масло сливочное'][0]['raw_values'][0])
        self.assertEqual('unknown',result['масло сливочное'][0]['food_state'])


if __name__=='__main__':unittest.main()
