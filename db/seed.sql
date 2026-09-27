-- Hand-maintained entities and aliases for the demo. TINs from the WPS MRF provider_references;
-- the type-2 NPI from the UW Health standard-charges file header.

INSERT INTO organizations (org_key, name, org_type, system_name, tin, address) VALUES
 ('uwhc', 'University of Wisconsin Hospital and Clinics Authority', 'hospital', 'UW Health', '391835630',
  '600 Highland Ave Madison, WI 53705|4602 Eastpark Blvd Madison, WI 53718|1675 Highland Ave Madison, WI 53705'),
 ('uwmf', 'University of Wisconsin Medical Foundation', 'physician_group', 'UW Health', '391824445', NULL)
ON CONFLICT DO NOTHING;

INSERT INTO org_npis (org_key, npi) VALUES ('uwhc', '1922043744') ON CONFLICT DO NOTHING;

INSERT INTO org_aliases (alias, org_key) VALUES
 ('uw health', 'uwhc'),
 ('uw health university hospital', 'uwhc'),
 ('university hospital', 'uwhc'),
 ('uw hospital and clinics', 'uwhc'),
 ('uw health east madison hospital', 'uwhc'),
 ('east madison hospital', 'uwhc'),
 ('american family children''s hospital', 'uwhc'),
 ('uw medical foundation', 'uwmf'),
 ('uw health physicians', 'uwmf')
ON CONFLICT DO NOTHING;

INSERT INTO payer_aliases (alias, payer_key) VALUES
 ('wps health solutions', 'wps'),
 ('wps health insurance', 'wps'),
 ('wps health plan', 'wps'),
 ('unitedhealthcare', 'united healthcare'),
 ('uhc', 'united healthcare'),
 ('anthem blue cross blue shield', 'anthem'),
 ('dean health plan', 'dean health plan')
ON CONFLICT DO NOTHING;
