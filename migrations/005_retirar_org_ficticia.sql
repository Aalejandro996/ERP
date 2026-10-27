-- 005: retira la «Organización principal» (J-00000000-0) creada por error por la migración 002 en una instalación nueva
-- (contaba como «datos antiguos» la bitácora de plataforma del Maestro). Idempotente y conservadora: solo actúa si esa
-- organización está VACÍA (sin empresas, usuarios, licencia ni equipos). Ejecutar con el rol DUEÑO.
DO $$
DECLARE o integer; n_nulos bigint;
BEGIN
  SELECT id INTO o FROM organizaciones
   WHERE nombre = 'Organización principal' AND rif = 'J-00000000-0'
     AND NOT EXISTS (SELECT 1 FROM empresas e WHERE e.organizacion_id = organizaciones.id)
     AND NOT EXISTS (SELECT 1 FROM usuarios u WHERE u.organizacion_id = organizaciones.id)
     AND NOT EXISTS (SELECT 1 FROM licencias_organizacion l WHERE l.organizacion_id = organizaciones.id AND l.datos::text <> '{}')
     AND NOT EXISTS (SELECT 1 FROM equipos q WHERE q.organizacion_id = organizaciones.id)
     AND EXISTS (SELECT 1 FROM usuarios m WHERE m.rol = 'Maestro');
  IF o IS NULL THEN RETURN; END IF;
  -- Los eventos del Maestro que se le asignaron vuelven a la bitácora de plataforma, solo si así la cadena sigue
  -- íntegra (es decir, si después no se registraron otros eventos de plataforma). Si no, quedan con su número
  -- original: la cadena de hashes no se altera ni se borra nada.
  SELECT count(*) INTO n_nulos FROM audit_log WHERE organizacion_id IS NULL;
  IF n_nulos = 0 THEN
    ALTER TABLE audit_log DISABLE TRIGGER trg_inmutable;
    UPDATE audit_log SET organizacion_id = NULL WHERE organizacion_id = o;
    ALTER TABLE audit_log ENABLE TRIGGER trg_inmutable;
  END IF;
  DELETE FROM tramos_provision WHERE organizacion_id = o;
  DELETE FROM licencias_organizacion WHERE organizacion_id = o;
  DELETE FROM organizaciones WHERE id = o;
  RAISE NOTICE 'Retirada la organización ficticia %', o;
END $$;
