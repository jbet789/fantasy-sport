-- 013: the limit of two F1 drivers per team is removed (commissioner's decision). The cap stays a setting rather than code: it is now
-- the size of a roster (20), which can never bind. To bring a limit back, update league_settings set f1_cap = <n>.
update league_settings set f1_cap = 20;
alter table league_settings alter column f1_cap set default 20;
